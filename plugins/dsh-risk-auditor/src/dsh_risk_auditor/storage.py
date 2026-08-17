"""Risk Auditor SQLite 持久化：审计结果重启可恢复。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


@contextmanager
def locked_conn():
    with _lock:
        yield get_conn()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.environ.get("RISK_AUDITOR_DB", ":memory:")
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        init_schema(_conn)
    return _conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS promotion_audits (
            audit_id          TEXT PRIMARY KEY,
            candidate_id      TEXT NOT NULL,
            strategy_id       TEXT NOT NULL,
            strategy_version  TEXT NOT NULL,
            evidence_hash     TEXT NOT NULL,
            approved          INTEGER NOT NULL,
            reason            TEXT NOT NULL,
            payload           TEXT NOT NULL,
            audited_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_promotion_audits_candidate
            ON promotion_audits (candidate_id);
        """
    )
    conn.commit()


def save_promotion_audit(record: dict) -> None:
    with locked_conn() as conn:
        conn.execute(
            """INSERT INTO promotion_audits
               (audit_id, candidate_id, strategy_id, strategy_version,
                evidence_hash, approved, reason, payload, audited_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["audit_id"],
                record["candidate_id"],
                record["strategy_id"],
                record["strategy_version"],
                record["evidence_hash"],
                1 if record["approved"] else 0,
                record["reason"],
                json.dumps(record, ensure_ascii=False),
                record["audited_at"],
            ),
        )
        conn.commit()


def get_promotion_audit(audit_id: str) -> dict | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM promotion_audits WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def reset() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
