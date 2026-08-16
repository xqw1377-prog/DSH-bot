"""SQLite 持久化层。

审批账本与幂等键日志不允许仅存内存：进程重启丢失审批意味着
已批准的资金动作无法追溯，丢失幂等键意味着重试可能产生双单。

通过 QUANT_GATEWAY_DB 指定数据库文件路径，未设置时退回内存
（仅限本地开发/测试）。失败关闭：数据库不可用时抛异常，
调用方必须拒绝资金动作而不是继续。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

# 单连接 + 全局锁串行化：FastAPI 默认线程池执行同步路由，
# sqlite3 连接不允许跨线程并发使用，所有访问必须持锁。
_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


@contextmanager
def locked_conn():
    with _lock:
        yield get_conn()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.environ.get("QUANT_GATEWAY_DB", ":memory:")
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        init_schema(_conn)
    return _conn


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
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
        """
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


def reset() -> None:
    """测试辅助：丢弃当前连接，恢复干净状态。"""
    global _conn
    if _conn is not None:
            _conn.close()
            _conn = None


def record_idempotency_key(key: str, request_hash: str) -> bool:
    """原子抢占幂等键（INSERT，主键唯一约束保证并发下只有一个成功）。

    抢占成功返回 True，调用方继续提交订单并通过 finalize 回填 order_id；
    键已存在返回 False（重复或并发请求）。
    """
    with locked_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash) VALUES (?, ?)",
                (key, request_hash),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def finalize_idempotency_key(key: str, order_id: str) -> None:
    """提交成功后回填权威订单 ID。"""
    with locked_conn() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET order_id = ? WHERE key = ?", (order_id, key)
        )
        conn.commit()


def get_idempotency_entry(key: str) -> tuple[str | None, str] | None:
    """返回 (order_id, request_hash)。order_id 为空表示有在途请求。"""
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT order_id, request_hash FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
    return (row[0], row[1]) if row else None


def get_order_id_for_key(key: str) -> str | None:
    entry = get_idempotency_entry(key)
    return entry[0] if entry else None
