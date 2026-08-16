"""SQLite 持久化层。

审批账本与幂等键日志不允许仅存内存：进程重启丢失审批意味着
已批准的资金动作无法追溯，丢失幂等键意味着重试可能产生双单。

通过 QUANT_GATEWAY_DB 指定数据库文件路径，未设置时退回内存
（仅限本地开发/测试）。失败关闭：数据库不可用时抛异常，
调用方必须拒绝资金动作而不是继续。
"""

import os
import sqlite3

# 单连接 + check_same_thread=False：FastAPI 默认线程池执行同步路由，
# SQLite 以串行写入为主，避免引入连接池复杂度。
_conn: sqlite3.Connection | None = None


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
            key        TEXT PRIMARY KEY,
            order_id   TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
        """
    )
    conn.commit()


def reset() -> None:
    """测试辅助：丢弃当前连接，恢复干净状态。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def record_idempotency_key(key: str, order_id: str) -> bool:
    """写入幂等键。键已存在时返回 False（重复提交），不覆盖原 order_id。"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (key, order_id) VALUES (?, ?)",
            (key, order_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_order_id_for_key(key: str) -> str | None:
    row = get_conn().execute(
        "SELECT order_id FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None
