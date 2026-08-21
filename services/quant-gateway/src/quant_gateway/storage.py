"""SQLite 持久化层。

审批账本与幂等键日志不允许仅存内存：进程重启丢失审批意味着
已批准的资金动作无法追溯，丢失幂等键意味着重试可能产生双单。

幂等键状态机（跨进程靠 SQLite 唯一约束 + BEGIN IMMEDIATE，不靠进程内锁）：
  RESERVED  → 抢占成功，尚未拿到 venue order_id
  SUBMITTED → venue 已返回 order_id，尚未标完成
  COMPLETED → 回填完成，可安全重放
  FAILED    → venue 提交失败，同 hash 允许重新抢占

通过 QUANT_GATEWAY_DB 指定数据库文件路径，未设置时退回内存
（仅限本地开发/测试）。失败关闭：数据库不可用时抛异常。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from dsh_contracts import Approval, ApprovalStatus

# 进程内串行化 + SQLite IMMEDIATE：覆盖同进程多线程；
# 多 worker / 多进程依赖 PRIMARY KEY + BEGIN IMMEDIATE。
_lock = threading.RLock()
_path: str | None = None




_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def _db_path() -> str:
    return os.environ.get("QUANT_GATEWAY_DB", ":memory:")


def _new_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)
    return conn


def get_conn() -> sqlite3.Connection:
    """仅用于 :memory: 模式的共享连接；文件模式请使用 locked_conn。"""
    global _conn
    if _conn is None:
        _conn = _new_conn(_db_path())
    return _conn


@contextmanager
def locked_conn():
    """一次操作一个连接。

    - 文件模式：独立新连接 + WAL + busy_timeout，多 worker / 多进程安全，
      写冲突由 SQLite 锁与唯一约束仲裁
    - :memory: 模式：共享连接 + 进程内全局锁（仅限单进程测试）
    """
    path = _db_path()
    if path == ":memory:":
        with _lock:
            yield get_conn()
        return
    conn = _new_conn(path)
    try:
        yield conn
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id   TEXT PRIMARY KEY,
            status        TEXT NOT NULL,
            market        TEXT NOT NULL,
            requested_at  TEXT NOT NULL,
            payload       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_status
            ON approvals (status);
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key          TEXT PRIMARY KEY,
            order_id     TEXT,
            request_hash TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'RESERVED',
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id    TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            service_principal TEXT,
            actor_principal   TEXT,
            actor       TEXT,
            action      TEXT NOT NULL,
            market      TEXT,
            subject_id  TEXT,
            outcome     TEXT NOT NULL,
            detail      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_occurred_at
            ON audit_log (occurred_at);
        CREATE TABLE IF NOT EXISTS risk_snapshots (
            risk_snapshot_id TEXT PRIMARY KEY,
            market           TEXT NOT NULL,
            payload          TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_orders (
            order_id  TEXT PRIMARY KEY,
            market    TEXT NOT NULL,
            payload   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_orders_idem
            ON paper_orders (json_extract(payload, '$.intent.idempotency_key'));
        """
    )
    # 迁移：旧表无 status 列时补上
    cols = {r[1] for r in conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()}
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE idempotency_keys ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETED'"
        )
        conn.execute(
            "UPDATE idempotency_keys SET status = CASE "
            "WHEN order_id IS NULL OR order_id = '' THEN 'RESERVED' "
            "ELSE 'COMPLETED' END"
        )
    if "updated_at" not in cols:
        conn.execute(
            "ALTER TABLE idempotency_keys ADD COLUMN updated_at TEXT "
            "NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
    audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    if "entry_hash" not in audit_cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN prev_hash TEXT")
        conn.execute("ALTER TABLE audit_log ADD COLUMN entry_hash TEXT")
        audit_cols |= {"prev_hash", "entry_hash"}
    added_service_principal = False
    if "service_principal" not in audit_cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN service_principal TEXT")
        added_service_principal = True
    if "actor_principal" not in audit_cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN actor_principal TEXT")
    if added_service_principal and "actor" in audit_cols:
        conn.execute(
            "UPDATE audit_log SET service_principal = COALESCE(service_principal, actor) "
            "WHERE service_principal IS NULL"
        )
    conn.commit()


def save_risk_snapshot(snapshot_id: str, market: str, payload: dict,
                       *, overwrite: bool = False) -> bool:
    """保存风险快照。

    默认不可变（INSERT-only）：同一 ID 不允许覆盖——快照是风控门禁的
    权威输入，被覆盖等于改写风控事实。返回是否真正写入。
    """
    import json

    with locked_conn() as conn:
        if overwrite:
            conn.execute(
                """INSERT INTO risk_snapshots (risk_snapshot_id, market, payload)
                   VALUES (?, ?, ?)
                   ON CONFLICT(risk_snapshot_id) DO UPDATE SET payload = excluded.payload""",
                (snapshot_id, market, json.dumps(payload, ensure_ascii=False)),
            )
            written = True
        else:
            cur = conn.execute(
                """INSERT INTO risk_snapshots (risk_snapshot_id, market, payload)
                   VALUES (?, ?, ?)
                   ON CONFLICT(risk_snapshot_id) DO NOTHING""",
                (snapshot_id, market, json.dumps(payload, ensure_ascii=False)),
            )
            written = cur.rowcount > 0
        conn.commit()
        return written


def get_risk_snapshot(snapshot_id: str) -> dict | None:
    import json

    with locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM risk_snapshots WHERE risk_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def save_paper_order(order_id: str, market: str, payload: dict) -> None:
    """订单 INSERT-only:同一 order_id 二次写入必须失败。

    订单是权威事实,静默覆盖等于改写成交历史;碰撞即 bug,让它响。
    """
    import json

    with locked_conn() as conn:
        conn.execute(
            """INSERT INTO paper_orders (order_id, market, payload)
               VALUES (?, ?, ?)""",
            (order_id, market, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def get_paper_order(order_id: str) -> dict | None:
    import json

    with locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM paper_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def find_paper_order_by_idempotency_key(key: str) -> dict | None:
    """崩溃恢复：venue 已下单但幂等键尚未 finalize 时，按 intent 反查。"""
    import json

    with locked_conn() as conn:
        rows = conn.execute("SELECT payload FROM paper_orders").fetchall()
    for (payload,) in rows:
        data = json.loads(payload)
        intent = data.get("intent") or {}
        if intent.get("idempotency_key") == key:
            return data
    return None


# TTL:各表保留窗口。审计日志是审计权威,永不清理(归档另议)。
TTL_IDEMPOTENCY_KEY_DAYS = 7       # 已完结(FINISHED)与已失败键
TTL_RISK_SNAPSHOT_HOURS = 25       # 快照有效期 600s,留足余量
TTL_TERMINAL_APPROVAL_DAYS = 30    # EXPIRED/REJECTED 终态审批

_last_prune_at: datetime | None = None
_PRUNE_INTERVAL = timedelta(hours=1)


def prune_expired(now: datetime | None = None) -> dict[str, int]:
    """按 TTL 清理已完成使命的行,返回各表删除数。

    - 幂等键:FINISHED/FAILED 且超窗(在途键绝不动)
    - 风险快照:超窗(不可变签发物,过期即无价值)
    - 审批:EXPIRED/REJECTED 终态且超窗(APPROVED/CONSUMING 绝不动)
    - 审计日志:不清理
    """
    current = now or datetime.now(UTC)
    idem_cutoff = (current - timedelta(days=TTL_IDEMPOTENCY_KEY_DAYS)).isoformat()
    snap_cutoff = (current - timedelta(hours=TTL_RISK_SNAPSHOT_HOURS)).isoformat()
    appr_cutoff = (current - timedelta(days=TTL_TERMINAL_APPROVAL_DAYS)).isoformat()
    removed: dict[str, int] = {}
    with locked_conn() as conn:
        cur = conn.execute(
            "DELETE FROM idempotency_keys WHERE status IN ('FINISHED','FAILED')"
            " AND updated_at < ?", (idem_cutoff,))
        removed["idempotency_keys"] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM risk_snapshots WHERE rowid IN ("
            "  SELECT rowid FROM risk_snapshots"
            "  WHERE json_extract(payload, '$.as_of') < ?)", (snap_cutoff,))
        removed["risk_snapshots"] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM approvals WHERE status IN ('EXPIRED','REJECTED')"
            " AND requested_at < ?", (appr_cutoff,))
        removed["approvals_terminal"] = cur.rowcount
        conn.commit()
    return removed


def maybe_prune(now: datetime | None = None) -> None:
    """时间戳守卫:至多每小时清理一次,写路径顺手调用,开销可忽略。"""
    global _last_prune_at
    current = now or datetime.now(UTC)
    if _last_prune_at is not None and current - _last_prune_at < _PRUNE_INTERVAL:
        return
    _last_prune_at = current
    try:
        prune_expired(now=current)
    except Exception:
        # 清理失败不阻断交易路径;下一窗口重试
        _last_prune_at = None


def reset() -> None:
    """测试辅助：丢弃内存连接，恢复干净状态。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def record_idempotency_key(key: str, request_hash: str) -> bool:
    """BEGIN IMMEDIATE 抢占幂等键为 RESERVED。

    成功返回 True；键已存在且非 FAILED 返回 False。
    FAILED 且同 hash 时删除旧行并重新抢占（允许失败后重试）。
    """
    with locked_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_hash, status, order_id FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                prev_hash, status, order_id = row
                if status == "FAILED" and prev_hash == request_hash:
                    conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                else:
                    conn.commit()
                    return False
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash, status) "
                "VALUES (?, ?, 'RESERVED')",
                (key, request_hash),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except Exception:
            conn.rollback()
            raise


def mark_idempotency_submitted(key: str, order_id: str) -> None:
    """venue 已返回 order_id：进入 SUBMITTED（崩溃后可凭 order_id 恢复）。"""
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE idempotency_keys SET order_id = ?, status = 'SUBMITTED', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
            (order_id, key),
        )
        conn.commit()


def finalize_idempotency_key(key: str, order_id: str) -> None:
    """提交成功后标 COMPLETED 并回填权威订单 ID。"""
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE idempotency_keys SET order_id = ?, status = 'COMPLETED', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
            (order_id, key),
        )
        finalize_consumed_approval(conn, key, order_id)
        conn.commit()


def mark_idempotency_failed(key: str) -> None:
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE idempotency_keys SET status = 'FAILED', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
            (key,),
        )
        release_consumed_approval(conn, key)
        conn.commit()


def get_idempotency_entry(key: str) -> tuple[str | None, str] | None:
    """兼容旧接口：返回 (order_id, request_hash)。"""
    full = get_idempotency_record(key)
    if full is None:
        return None
    return full["order_id"], full["request_hash"]


def get_idempotency_record(key: str) -> dict[str, Any] | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT order_id, request_hash, status, updated_at"
            " FROM idempotency_keys WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return {"order_id": row[0], "request_hash": row[1],
            "status": row[2], "updated_at": row[3]}


def get_order_id_for_key(key: str) -> str | None:
    entry = get_idempotency_entry(key)
    return entry[0] if entry else None


def release_consumed_approval(conn: sqlite3.Connection, idempotency_key: str) -> None:
    """把已消费的审批释放回 APPROVED（幂等键失败后允许按新单重试）。

    必须与幂等键 FAILED 标记在同一事务内调用（见 mark_idempotency_failed），
    保证「键释放 + 审批回滚」原子。
    """
    row = conn.execute(
        """SELECT approval_id, payload FROM approvals
           WHERE json_extract(payload, '$.consumed_key') = ?""",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return
    approval = Approval.model_validate_json(row[1])
    if approval.status != ApprovalStatus.CONSUMING:
        return
    updated = approval.model_copy(update={
        "status": ApprovalStatus.APPROVED,
        "consumed_key": None,
        "consumed_request_hash": None,
        "consumed_at": None,
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    })
    conn.execute(
        "UPDATE approvals SET status = ?, payload = ? WHERE approval_id = ?",
        (updated.status.value, updated.model_dump_json(), approval.approval_id),
    )


def finalize_consumed_approval(conn: sqlite3.Connection, idempotency_key: str, order_id: str) -> None:
    """消费完成：审批进入 CONSUMED 并回填权威订单 ID。

    必须与幂等键 COMPLETED 标记在同一事务内调用（见 finalize_idempotency_key）。
    """
    row = conn.execute(
        """SELECT approval_id, payload FROM approvals
           WHERE json_extract(payload, '$.consumed_key') = ?""",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return
    approval = Approval.model_validate_json(row[1])
    if approval.status != ApprovalStatus.CONSUMING:
        return
    updated = approval.model_copy(update={
        "status": ApprovalStatus.CONSUMED,
        "consumed_order_id": order_id,
    })
    conn.execute(
        "UPDATE approvals SET status = ?, payload = ? WHERE approval_id = ?",
        (updated.status.value, updated.model_dump_json(), approval.approval_id),
    )
