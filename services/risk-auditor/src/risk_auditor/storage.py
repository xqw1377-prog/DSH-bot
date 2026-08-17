"""Risk Auditor 独立存储边界。

与 strategy-evolution / gateway 完全分库（RISK_AUDITOR_DB），
只保存审计结论，不保存任何交易凭据或账户数据。
结论按（candidate, to_stage, evidence_hash）幂等。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db_path() -> str:
    return os.environ.get("RISK_AUDITOR_DB", ":memory:")


@contextmanager
def locked_conn():
    path = _db_path()
    if path == ":memory:":
        global _conn
        with _lock:
            if _conn is None:
                _conn = sqlite3.connect(":memory:", check_same_thread=False)
                _conn.executescript(_SCHEMA)
            yield _conn
        return
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL,
    to_stage      TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    reason        TEXT,
    strategy_version TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (candidate_id, to_stage, evidence_hash)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def evidence_hash(refs: list[str]) -> str:
    return sha256("\n".join(sorted(set(refs))).encode()).hexdigest()


def find_conclusion(candidate_id: str, to_stage: str,
                    ev_hash: str) -> dict | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT conclusion_id, verdict, reason, strategy_version,"
            " created_at FROM conclusions"
            " WHERE candidate_id = ? AND to_stage = ? AND evidence_hash = ?",
            (candidate_id, to_stage, ev_hash)).fetchone()
    if row is None:
        return None
    return {"conclusion_id": row[0], "verdict": row[1], "reason": row[2],
            "strategy_version": row[3], "created_at": row[4]}


def save_conclusion(candidate_id: str, to_stage: str, ev_hash: str,
                    verdict: str, reason: str,
                    strategy_version: str) -> dict:
    with locked_conn() as conn:
        conclusion_id = f"audit-{uuid4().hex[:12]}"
        conn.execute(
            "INSERT OR IGNORE INTO conclusions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conclusion_id, candidate_id, to_stage, ev_hash, verdict, reason,
             strategy_version, _now()),
        )
        conn.commit()
    found = find_conclusion(candidate_id, to_stage, ev_hash)
    assert found is not None
    return found


def reset() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
