"""确定性事故中心（Incident Center）。

事故处理是确定性流程，不依赖 LLM：
- 指纹去重：同一 fingerprint（source+reason+subject）的重复上报合并到
  同一事故（occurrences 递增），不产生重复事故
- 生命周期状态机：OPEN → MITIGATED → RESOLVED；非法迁移 422
- append-only 时间线：每次状态变化与上报都留痕，可审计
- 存储（INCIDENT_CENTER_DB）独立于交易链路；本服务没有任何资金接口
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Incident Center",
    description="确定性事故中心：去重、生命周期、时间线。与交易链路隔离。",
    version="0.1.0",
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id  TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    market       TEXT,
    severity     TEXT NOT NULL DEFAULT "NORMAL",
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT "OPEN",
    occurrences  INTEGER NOT NULL DEFAULT 1,
    opened_at    TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline (
    entry_id     TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    detail       TEXT
);
"""

_TRANSITIONS = {"OPEN": {"MITIGATED"}, "MITIGATED": {"RESOLVED", "OPEN"},
                "RESOLVED": set()}


def _db_path() -> str:
    return os.environ.get("INCIDENT_CENTER_DB", ":memory:")


@contextmanager
def locked_conn():
    if _db_path() == ":memory:":
        global _conn
        with _lock:
            if _conn is None:
                _conn = sqlite3.connect(":memory:", check_same_thread=False)
                _conn.executescript(_SCHEMA)
                _conn.commit()
            yield _conn
        return
    conn = sqlite3.connect(_db_path(), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def reset() -> None:
    """测试辅助：丢弃内存连接。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(source: str, reason: str, subject: str | None) -> str:
    return sha256(f"{source}|{reason}|{subject or ''}".encode()).hexdigest()


def _timeline(conn, incident_id: str, action: str, actor: str,
              detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO timeline VALUES (?, ?, ?, ?, ?, ?)",
        (f"tl-{uuid4().hex[:12]}", incident_id, _now(), action, actor, detail),
    )


class IncidentOpen(BaseModel):
    source: str                 # 上报方（bot / service 名）
    reason: str
    market: str | None = None
    subject: str | None = None  # 关联对象（order_id / candidate_id…）
    severity: str = "NORMAL"    # NORMAL | HIGH


class IncidentAction(BaseModel):
    actor: str
    note: str | None = None


@app.post("/v1/incidents", status_code=201)
def open_incident(req: IncidentOpen) -> dict:
    fp = _fingerprint(req.source, req.reason, req.subject)
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT incident_id, status, occurrences FROM incidents"
            " WHERE fingerprint = ?", (fp,)).fetchone()
        if row is not None:
            incident_id, status, occurrences = row
            # 同指纹重复上报：合并计数，不新建；已解决的事故重新打开
            conn.execute(
                "UPDATE incidents SET occurrences = ?, status = ?,"
                " updated_at = ? WHERE incident_id = ?",
                (occurrences + 1,
                 "OPEN" if status == "RESOLVED" else status,
                 _now(), incident_id),
            )
            _timeline(conn, incident_id, "reopened" if status == "RESOLVED"
                      else "re-reported", req.source,
                      f"occurrences={occurrences + 1}")
            conn.commit()
            return _get_incident(conn, incident_id)
        incident_id = f"inc-{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO incidents (incident_id, fingerprint, source, market,"
            " severity, reason, opened_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, fp, req.source, req.market, req.severity,
             req.reason, _now(), _now()),
        )
        _timeline(conn, incident_id, "opened", req.source, req.reason)
        conn.commit()
        return _get_incident(conn, incident_id)


def _get_incident(conn, incident_id: str) -> dict:
    row = conn.execute(
        "SELECT incident_id, source, market, severity, reason, status,"
        " occurrences, opened_at, updated_at FROM incidents"
        " WHERE incident_id = ?", (incident_id,)).fetchone()
    keys = ("incident_id", "source", "market", "severity", "reason",
            "status", "occurrences", "opened_at", "updated_at")
    return dict(zip(keys, row))


@app.get("/v1/incidents")
def list_incidents(status: str | None = None,
                   market: str | None = None) -> list[dict]:
    with locked_conn() as conn:
        sql = "SELECT incident_id FROM incidents"
        conds, params = [], []
        if status:
            conds.append("status = ?"); params.append(status)
        if market:
            conds.append("market = ?"); params.append(market)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = conn.execute(sql, *([params] if params else [])).fetchall()
        # sqlite3 execute 参数需要序列
        out = [_get_incident(conn, r[0]) for r in rows]
    return out


@app.get("/v1/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    with locked_conn() as conn:
        try:
            return _get_incident(conn, incident_id)
        except Exception:
            raise HTTPException(status_code=404, detail="incident not found")


@app.post("/v1/incidents/{incident_id}/mitigate")
def mitigate(incident_id: str, req: IncidentAction) -> dict:
    return _transition(incident_id, "MITIGATED", req)


@app.post("/v1/incidents/{incident_id}/resolve")
def resolve(incident_id: str, req: IncidentAction) -> dict:
    return _transition(incident_id, "RESOLVED", req)


def _transition(incident_id: str, target: str, req: IncidentAction) -> dict:
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _get_incident(conn, incident_id)["status"]
        except Exception:
            conn.rollback()
            raise HTTPException(status_code=404, detail="incident not found")
        if target not in _TRANSITIONS[current]:
            conn.rollback()
            raise HTTPException(
                status_code=422,
                detail=f"illegal transition {current} -> {target}",
            )
        conn.execute(
            "UPDATE incidents SET status = ?, updated_at = ?"
            " WHERE incident_id = ?", (target, _now(), incident_id))
        _timeline(conn, incident_id, target.lower(), req.actor, req.note)
        conn.commit()
        return _get_incident(conn, incident_id)


@app.get("/v1/incidents/{incident_id}/timeline")
def timeline(incident_id: str) -> list[dict]:
    with locked_conn() as conn:
        rows = conn.execute(
            "SELECT entry_id, occurred_at, action, actor, detail FROM timeline"
            " WHERE incident_id = ? ORDER BY occurred_at", (incident_id,),
        ).fetchall()
    keys = ("entry_id", "occurred_at", "action", "actor", "detail")
    return [dict(zip(keys, r)) for r in rows]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "incident-center"}
