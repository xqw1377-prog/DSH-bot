"""情报库。独立 sqlite，不碰 Gateway / runtime 交易库。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from intelligence_ingest.documents import Document, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_tier TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    event_time TEXT,
    edited_at TEXT,
    content_hash TEXT NOT NULL UNIQUE,
    language TEXT,
    raw_text TEXT NOT NULL,
    assets TEXT NOT NULL,
    collection_method TEXT NOT NULL,
    title TEXT,
    market TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_documents INTEGER NOT NULL DEFAULT 0,
    last_recovery_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    affected_assets TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence TEXT NOT NULL,
    impact_horizon TEXT NOT NULL,
    impact_score TEXT,
    mode TEXT NOT NULL,
    can_apply INTEGER NOT NULL,
    evidence_refs TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class IntelligenceStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("INTELLIGENCE_DB") or ":memory:"
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert_document(self, doc: Document) -> Document:
        self._conn.execute(
            """
            INSERT INTO documents (
                document_id, source_id, source_tier, canonical_url, published_at,
                fetched_at, event_time, edited_at, content_hash, language, raw_text,
                assets, collection_method, title, market
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET fetched_at=excluded.fetched_at
            """,
            (
                doc.document_id,
                doc.source_id,
                doc.source_tier,
                doc.canonical_url,
                doc.published_at,
                doc.fetched_at,
                doc.event_time,
                doc.edited_at,
                doc.content_hash,
                doc.language,
                doc.raw_text,
                json.dumps(doc.assets, ensure_ascii=False),
                doc.collection_method,
                doc.title,
                doc.market,
            ),
        )
        self._conn.commit()
        return doc

    def upsert_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self._conn.execute(
            """
            INSERT INTO events (
                event_id, document_id, event_type, affected_assets, direction,
                confidence, impact_horizon, impact_score, mode, can_apply,
                evidence_refs, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload
            """,
            (
                event["event_id"],
                event.get("document_id") or "",
                event["event_type"],
                json.dumps(event.get("affected_assets") or [], ensure_ascii=False),
                event["direction"],
                str(event["confidence"]),
                event["impact_horizon"],
                str(event.get("impact_score") or ""),
                event["mode"],
                1 if event.get("can_apply") else 0,
                json.dumps(event.get("evidence_refs") or [], ensure_ascii=False),
                json.dumps(event, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return event

    def record_source_result(self, source_id: str, *, ok: bool,
                             error: str | None = None,
                             documents: int = 0,
                             now: str | None = None) -> dict[str, Any]:
        """记录一次源采集结果;成功且此前连续失败>0 视为恢复(记 last_recovery_at)。

        采集源是 RSS/Atom 这类「拉最近条目」的模型:成功的那一拉本身就是
        补采——恢复即补采,无需独立的 backfill 通道。
        """
        from intelligence_ingest.documents import utc_now

        ts = now or utc_now()
        row = self._conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        prev_failures = row["consecutive_failures"] if row else 0
        recovered = ok and prev_failures > 0
        self._conn.execute(
            """
            INSERT INTO source_health (
                source_id, last_attempt_at, last_success_at, last_failure_at,
                consecutive_failures, last_error, last_documents, last_recovery_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = COALESCE(excluded.last_success_at, source_health.last_success_at),
                last_failure_at = COALESCE(excluded.last_failure_at, source_health.last_failure_at),
                consecutive_failures = excluded.consecutive_failures,
                last_error = excluded.last_error,
                last_documents = excluded.last_documents,
                last_recovery_at = COALESCE(excluded.last_recovery_at, source_health.last_recovery_at)
            """,
            (
                source_id, ts,
                ts if ok else None,
                None if ok else ts,
                0 if ok else prev_failures + 1,
                None if ok else (error or "unknown error")[:500],
                documents if ok else 0,
                ts if recovered else None,
            ),
        )
        self._conn.commit()
        return self.get_source_health(source_id)

    def get_source_health(self, source_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM source_health WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_source_health(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM source_health ORDER BY source_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_documents(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM documents ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_doc_row(row) for row in rows]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                continue
        return out

    def export_snapshot(self, dest: Path) -> dict[str, Any]:
        from intelligence_ingest.documents import utc_now

        payload = {
            "schema_version": "dsh-intelligence-1",
            "exported_at": utc_now(),
            "mode": "SHADOW",
            "disclaimer": "只进入 Shadow。没有原文存证的记录不会评分。不能直接下单。",
            "documents": self.recent_documents(80),
            "events": self.recent_events(80),
            "source_health": self.list_source_health(),
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dest)
        return payload


def _doc_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["assets"] = json.loads(data.get("assets") or "[]")
    except json.JSONDecodeError:
        data["assets"] = []
    return data


# TTL:24h 采集下增长最快的两张表。原文存证是验收要求,窗口放宽到 180 天。
TTL_DAYS = 180
_last_prune_at: str | None = None


def prune_expired(store: IntelligenceStore, now: str | None = None,
                  *, ttl_days: int = TTL_DAYS) -> dict[str, int]:
    """按 TTL 清理文档与事件,返回删除数。事件先于文档(引用完整性)。"""
    from datetime import datetime, timedelta

    current = datetime.fromisoformat((now or utc_now()).replace("Z", "+00:00"))
    cutoff = (current - timedelta(days=ttl_days)).isoformat()
    removed: dict[str, int] = {}
    cur = store._conn.execute(
        "DELETE FROM events WHERE document_id IN "
        "(SELECT document_id FROM documents WHERE fetched_at < ?)", (cutoff,))
    removed["events"] = cur.rowcount
    cur = store._conn.execute("DELETE FROM documents WHERE fetched_at < ?", (cutoff,))
    removed["documents"] = cur.rowcount
    store._conn.commit()
    return removed


def maybe_prune(store: IntelligenceStore, now: str | None = None) -> None:
    """至多每 24h 清理一次;ingest_once 顺手调用。"""
    global _last_prune_at
    from datetime import datetime, timedelta

    current = now or utc_now()
    if _last_prune_at is not None:
        try:
            prev = datetime.fromisoformat(_last_prune_at.replace("Z", "+00:00"))
            if (datetime.fromisoformat(current.replace("Z", "+00:00"))
                    - prev) < timedelta(hours=24):
                return
        except ValueError:
            pass
    _last_prune_at = current
    try:
        prune_expired(store, now=current)
    except Exception:
        _last_prune_at = None


def _doc_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["assets"] = json.loads(data.get("assets") or "[]")
    except json.JSONDecodeError:
        data["assets"] = []
    return data
