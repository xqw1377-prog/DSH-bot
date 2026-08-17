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
from typing import Any

# 进程内串行化 + SQLite IMMEDIATE：覆盖同进程多线程；
# 多 worker / 多进程依赖 PRIMARY KEY + BEGIN IMMEDIATE。
_lock = threading.RLock()
_path: str | None = None


def _db_path() -> str:
    return os.environ.get("QUANT_GATEWAY_DB", ":memory:")


def _connect() -> sqlite3.Connection:
    """每请求可取独立连接；:memory: 必须共享同一连接否则丢表。"""
    global _path
    path = _db_path()
    if path == ":memory:":
        # 内存库：模块级单连接（测试）
        if not hasattr(_connect, "_mem"):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            init_schema(conn)
            _connect._mem = conn  # type: ignore[attr-defined]
        return _connect._mem  # type: ignore[attr-defined]
    if _path != path:
        _path = path
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_schema(conn)
    return conn


@contextmanager
def locked_conn():
    with _lock:
        conn = _connect()
        try:
            yield conn
        finally:
            if _db_path() != ":memory:":
                conn.close()


def get_conn() -> sqlite3.Connection:
    """兼容旧调用：返回可用连接（内存模式为共享连接）。"""
    return _connect()


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
            actor       TEXT NOT NULL,
            action      TEXT NOT NULL,
            market      TEXT,
            subject_id  TEXT,
            outcome     TEXT NOT NULL,
            detail      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_occurred_at
            ON audit_log (occurred_at);
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
    conn.commit()


def save_paper_order(order_id: str, market: str, payload: dict) -> None:
    import json

    with locked_conn() as conn:
        conn.execute(
            """INSERT INTO paper_orders (order_id, market, payload)
               VALUES (?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET payload = excluded.payload""",
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


def reset() -> None:
    """测试辅助：丢弃当前连接，恢复干净状态。"""
    global _path
    with _lock:
        if hasattr(_connect, "_mem"):
            try:
                _connect._mem.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            delattr(_connect, "_mem")
        _path = None


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
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_keys SET order_id = ?, status = 'SUBMITTED', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
                (order_id, key),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def finalize_idempotency_key(key: str, order_id: str) -> None:
    """提交成功后标 COMPLETED 并回填权威订单 ID。"""
    with locked_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_keys SET order_id = ?, status = 'COMPLETED', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
                (order_id, key),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def mark_idempotency_failed(key: str) -> None:
    with locked_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_keys SET status = 'FAILED', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE key = ?",
                (key,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_idempotency_entry(key: str) -> tuple[str | None, str] | None:
    """兼容旧接口：返回 (order_id, request_hash)。"""
    full = get_idempotency_record(key)
    if full is None:
        return None
    return full["order_id"], full["request_hash"]


def get_idempotency_record(key: str) -> dict[str, Any] | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT order_id, request_hash, status FROM idempotency_keys WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return {"order_id": row[0], "request_hash": row[1], "status": row[2]}


def get_order_id_for_key(key: str) -> str | None:
    entry = get_idempotency_entry(key)
    return entry[0] if entry else None
