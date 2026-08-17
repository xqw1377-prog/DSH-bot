"""SQLite 持久化层：实验账本与策略候选。

实验账本与候选状态不允许仅存内存（PRD 10.4）：进程重启丢失意味着
已验证的策略候选无法追溯，策略晋级历史断档。

通过 STRATEGY_EVOLUTION_DB 指定数据库文件路径，未设置时退回内存
（仅限本地开发/测试）。失败关闭：数据库不可用时抛异常。
"""

import os
import sqlite3

_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.environ.get("STRATEGY_EVOLUTION_DB", ":memory:")
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        init_schema(_conn)
    return _conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id    TEXT PRIMARY KEY,
            market           TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            hypothesis       TEXT NOT NULL,
            data_snapshot_id TEXT NOT NULL,
            status           TEXT NOT NULL,
            created_by_bot   TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            result_ref       TEXT,
            payload          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_experiments_market
            ON experiments (market);
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id     TEXT PRIMARY KEY,
            market            TEXT NOT NULL,
            strategy_id       TEXT NOT NULL,
            strategy_version  TEXT NOT NULL,
            stage             TEXT NOT NULL,
            experiment_id     TEXT,
            evidence_refs     TEXT NOT NULL DEFAULT '[]',
            approval_id       TEXT,
            updated_at        TEXT NOT NULL,
            payload           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_market
            ON candidates (market);
        CREATE INDEX IF NOT EXISTS idx_candidates_stage
            ON candidates (stage);
        """
    )
    conn.commit()


def reset() -> None:
    """测试辅助：丢弃当前连接，恢复干净状态。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
