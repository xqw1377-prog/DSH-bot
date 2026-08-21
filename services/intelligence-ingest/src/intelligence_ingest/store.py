"""情报库。独立 sqlite，不碰 Gateway / runtime 交易库。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from intelligence_ingest.documents import Document

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
