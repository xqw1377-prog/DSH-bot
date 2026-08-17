"""Strategy Evolution 持久化账本。

实验、候选、证据与审计历史全部落 SQLite（STRATEGY_EVOLUTION_DB）：
- 候选带 version 乐观锁：并发晋级只有一个成功
- 证据 append-only：每条 ref 独立记录，晋级时校验整体哈希防篡改
- 审计历史 append-only：每次状态迁移留痕（含 evidence_hash 与审批 ID）
- 文件库每操作独立连接 + WAL + busy_timeout（与 Gateway 同一套多进程约定）
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
    return os.environ.get("STRATEGY_EVOLUTION_DB", ":memory:")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if path == ":memory:":
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY,
            market        TEXT NOT NULL,
            payload       TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id  TEXT PRIMARY KEY,
            market        TEXT NOT NULL,
            stage         TEXT NOT NULL,
            version       INTEGER NOT NULL DEFAULT 1,
            payload       TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
            candidate_id TEXT NOT NULL,
            seq          INTEGER NOT NULL,
            ref          TEXT NOT NULL,
            added_at     TEXT NOT NULL,
            PRIMARY KEY (candidate_id, seq)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id      TEXT PRIMARY KEY,
            occurred_at   TEXT NOT NULL,
            action        TEXT NOT NULL,
            candidate_id  TEXT,
            from_stage    TEXT,
            to_stage      TEXT,
            evidence_hash TEXT,
            approval_id   TEXT,
            detail        TEXT
        );
        """
    )
    conn.commit()
    return conn


@contextmanager
def locked_conn():
    path = _db_path()
    if path == ":memory:":
        with _lock:
            yield _get()
        return
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def reset() -> None:
    """测试辅助：丢弃内存连接。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---- 实验 ----

def save_experiment(experiment_id: str, market: str, payload: dict) -> None:
    with locked_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO experiments VALUES (?, ?, ?, ?)",
            (experiment_id, market, json.dumps(payload, ensure_ascii=False),
             _now()),
        )
        conn.commit()


def list_experiments(market: str | None = None) -> list[dict]:
    with locked_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT payload FROM experiments WHERE market = ?",
                (market,)).fetchall()
        else:
            rows = conn.execute("SELECT payload FROM experiments").fetchall()
    return [json.loads(r[0]) for r in rows]


def get_experiment(experiment_id: str) -> dict | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM experiments WHERE experiment_id = ?",
            (experiment_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ---- 候选（乐观锁） ----

def save_candidate(candidate_id: str, market: str, stage: str, payload: dict,
                   version: int = 1) -> None:
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO candidates"
            " (candidate_id, market, stage, version, payload, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (candidate_id, market, stage, version,
             json.dumps(payload, ensure_ascii=False), _now()),
        )
        conn.commit()


def get_candidate(candidate_id: str) -> dict | None:
    with locked_conn() as conn:
        row = conn.execute(
            "SELECT payload, version, stage FROM candidates"
            " WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if row is None:
        return None
    out = json.loads(row[0])
    out["_version"] = row[1]
    out["_stage"] = row[2]
    return out


def update_candidate_stage(candidate_id: str, stage: str, payload: dict,
                           expected_version: int) -> bool:
    """乐观锁更新：版本不匹配返回 False（409 冲突）。"""
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE candidates SET stage = ?, version = version + 1,"
            " payload = ?, updated_at = ?"
            " WHERE candidate_id = ? AND version = ?",
            (stage, json.dumps(payload, ensure_ascii=False), _now(),
             candidate_id, expected_version),
        )
        conn.commit()
        return cur.rowcount == 1


# ---- 证据（append-only + 哈希防篡改） ----

def append_evidence(candidate_id: str, refs: list[str]) -> None:
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM evidence WHERE candidate_id = ?",
            (candidate_id,)).fetchone()
        seq = seq_row[0]
        for ref in refs:
            seq += 1
            conn.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?)",
                (candidate_id, seq, ref, _now()),
            )
        conn.commit()


def evidence_refs(candidate_id: str) -> list[str]:
    with locked_conn() as conn:
        rows = conn.execute(
            "SELECT ref FROM evidence WHERE candidate_id = ? ORDER BY seq",
            (candidate_id,)).fetchall()
    return [r[0] for r in rows]


def evidence_hash(refs: list[str]) -> str:
    """证据列表的规范哈希：排序去重后拼接。防篡改锚点。"""
    return sha256("\n".join(sorted(set(refs))).encode()).hexdigest()


# ---- 审计历史（append-only） ----

def audit(action: str, candidate_id: str | None = None,
          from_stage: str | None = None, to_stage: str | None = None,
          evidence_hash: str | None = None, approval_id: str | None = None,
          detail: str | None = None) -> None:
    with locked_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"audit-{uuid4().hex[:12]}", _now(), action, candidate_id,
             from_stage, to_stage, evidence_hash, approval_id, detail),
        )
        conn.commit()


def audit_history(candidate_id: str | None = None) -> list[dict]:
    with locked_conn() as conn:
        if candidate_id:
            rows = conn.execute(
                "SELECT audit_id, occurred_at, action, candidate_id,"
                " from_stage, to_stage, evidence_hash, approval_id, detail"
                " FROM audit_log WHERE candidate_id = ? ORDER BY occurred_at",
                (candidate_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT audit_id, occurred_at, action, candidate_id,"
                " from_stage, to_stage, evidence_hash, approval_id, detail"
                " FROM audit_log ORDER BY occurred_at").fetchall()
    keys = ("audit_id", "occurred_at", "action", "candidate_id",
            "from_stage", "to_stage", "evidence_hash", "approval_id",
            "detail")
    return [dict(zip(keys, r)) for r in rows]
