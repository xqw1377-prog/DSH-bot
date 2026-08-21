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

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from incident_center.service_auth import require_service_key

app = FastAPI(
    title="Incident Center",
    description="确定性事故中心：去重、生命周期、时间线。与交易链路隔离。",
    version="0.1.0",
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,   -- 同类事故合并键（不含自由文本）
    source          TEXT NOT NULL,
    market          TEXT,
    incident_type   TEXT NOT NULL,   -- 规则/类型 ID（如 order_unknown_quarantine）
    severity        TEXT NOT NULL DEFAULT "NORMAL",
    reason          TEXT,            -- 仅描述，不参与指纹
    status          TEXT NOT NULL DEFAULT "OPEN",
    occurrences     INTEGER NOT NULL DEFAULT 1,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reported_events (
    source_event_id TEXT PRIMARY KEY,   -- 消息级幂等：同一事件重复投递直接忽略
    incident_id     TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
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


def _fingerprint(source: str, incident_type: str, market: str | None,
                 subject: str | None) -> str:
    """同类事故合并键：source_service + incident_type + market + subject。

    刻意不含自由文本 reason：描述措辞变化不应产生新事故。
    """
    return sha256(
        f"{source}|{incident_type}|{market or ''}|{subject or ''}".encode()
    ).hexdigest()


def _timeline(conn, incident_id: str, action: str, actor: str,
              detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO timeline VALUES (?, ?, ?, ?, ?, ?)",
        (f"tl-{uuid4().hex[:12]}", incident_id, _now(), action, actor, detail),
    )


class IncidentOpen(BaseModel):
    source: str                     # 上报方（bot / service 名）
    incident_type: str              # 规则/类型 ID，参与指纹
    reason: str | None = None       # 仅描述，不参与指纹
    market: str | None = None
    subject: str | None = None      # 关联对象（order_id / candidate_id…）
    severity: str = "NORMAL"        # NORMAL | HIGH
    source_event_id: str | None = None  # 消息级幂等键（如 Runtime event_id）


class IncidentAction(BaseModel):
    actor: str
    note: str | None = None


@app.post("/v1/incidents", status_code=201, dependencies=[Depends(require_service_key)])
def open_incident(req: IncidentOpen) -> dict:
    """上报语义（消息幂等与指纹合并分离）：

    - 相同 source_event_id 重复投递 → 完全幂等：不改计数、不写时间线
    - 新 source_event_id + 已有指纹   → occurrences + 1
    - RESOLVED 后重放旧 event_id     → 保持 RESOLVED
    - RESOLVED 后出现新 event_id     → REOPENED（计数继续累计）
    """
    fp = _fingerprint(req.source, req.incident_type, req.market, req.subject)
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")

        # 消息级幂等：同一事件已处理过，直接返回现状
        if req.source_event_id:
            seen = conn.execute(
                "SELECT incident_id FROM reported_events"
                " WHERE source_event_id = ?", (req.source_event_id,)).fetchone()
            if seen is not None:
                conn.rollback()
                incident = _get_incident(conn, seen[0])
                incident["deduplicated"] = "event"
                return incident

        row = conn.execute(
            "SELECT incident_id, status, occurrences FROM incidents"
            " WHERE fingerprint = ?", (fp,)).fetchone()
        if row is not None:
            incident_id, status, occurrences = row
            if status == "RESOLVED":
                # 新事件使同类事故重新打开
                conn.execute(
                    "UPDATE incidents SET status = 'OPEN',"
                    " occurrences = ?, updated_at = ? WHERE incident_id = ?",
                    (occurrences + 1, _now(), incident_id))
                _timeline(conn, incident_id, "reopened", req.source,
                          f"occurrences={occurrences + 1}; {req.reason or ''}")
            else:
                conn.execute(
                    "UPDATE incidents SET occurrences = ?, updated_at = ?"
                    " WHERE incident_id = ?",
                    (occurrences + 1, _now(), incident_id))
                _timeline(conn, incident_id, "re-reported", req.source,
                          f"occurrences={occurrences + 1}; {req.reason or ''}")
        else:
            incident_id = f"inc-{uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO incidents (incident_id, fingerprint, source,"
                " market, incident_type, severity, reason, opened_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (incident_id, fp, req.source, req.market, req.incident_type,
                 req.severity, req.reason, _now(), _now()),
            )
            _timeline(conn, incident_id, "opened", req.source, req.reason)

        if req.source_event_id:
            conn.execute(
                "INSERT INTO reported_events VALUES (?, ?, ?)",
                (req.source_event_id, incident_id, _now()),
            )
        conn.commit()
        incident = _get_incident(conn, incident_id)
        incident["deduplicated"] = "none"
        return incident


def _get_incident(conn, incident_id: str) -> dict:
    row = conn.execute(
        "SELECT incident_id, source, market, incident_type, severity,"
        " reason, status, occurrences, opened_at, updated_at FROM incidents"
        " WHERE incident_id = ?", (incident_id,)).fetchone()
    keys = ("incident_id", "source", "market", "incident_type", "severity",
            "reason", "status", "occurrences", "opened_at", "updated_at")
    return dict(zip(keys, row, strict=False))


@app.get("/v1/incidents", dependencies=[Depends(require_service_key)])
def list_incidents(status: str | None = None,
                   market: str | None = None) -> list[dict]:
    with locked_conn() as conn:
        sql = "SELECT incident_id FROM incidents"
        conds, params = [], []
        if status:
            conds.append("status = ?")
            params.append(status)
        if market:
            conds.append("market = ?")
            params.append(market)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = conn.execute(sql, *([params] if params else [])).fetchall()
        # sqlite3 execute 参数需要序列
        out = [_get_incident(conn, r[0]) for r in rows]
    return out


@app.get("/v1/incidents/{incident_id}", dependencies=[Depends(require_service_key)])
def get_incident(incident_id: str) -> dict:
    with locked_conn() as conn:
        try:
            return _get_incident(conn, incident_id)
        except Exception:
            raise HTTPException(status_code=404, detail="incident not found") from None


@app.post("/v1/incidents/{incident_id}/mitigate", dependencies=[Depends(require_service_key)])
def mitigate(incident_id: str, req: IncidentAction) -> dict:
    return _transition(incident_id, "MITIGATED", req)


@app.post("/v1/incidents/{incident_id}/resolve", dependencies=[Depends(require_service_key)])
def resolve(incident_id: str, req: IncidentAction) -> dict:
    return _transition(incident_id, "RESOLVED", req)


def _transition(incident_id: str, target: str, req: IncidentAction) -> dict:
    with locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _get_incident(conn, incident_id)["status"]
        except Exception:
            conn.rollback()
            raise HTTPException(status_code=404, detail="incident not found") from None
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


@app.get("/v1/incidents/{incident_id}/timeline", dependencies=[Depends(require_service_key)])
def timeline(incident_id: str) -> list[dict]:
    with locked_conn() as conn:
        rows = conn.execute(
            "SELECT entry_id, occurred_at, action, actor, detail FROM timeline"
            " WHERE incident_id = ? ORDER BY occurred_at", (incident_id,),
        ).fetchall()
    keys = ("entry_id", "occurred_at", "action", "actor", "detail")
    return [dict(zip(keys, r, strict=False)) for r in rows]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "incident-center"}
