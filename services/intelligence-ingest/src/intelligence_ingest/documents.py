"""原文存证。没有 URL、发布时间、原文和哈希的记录不能进入影响判断。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def content_hash(*, canonical_url: str, published_at: str, raw_text: str) -> str:
    payload = f"{canonical_url}\n{published_at}\n{raw_text}".encode()
    return sha256(payload).hexdigest()


@dataclass
class Document:
    document_id: str
    source_id: str
    source_tier: str
    canonical_url: str
    published_at: str
    fetched_at: str
    content_hash: str
    language: str
    raw_text: str
    assets: list[str]
    collection_method: str
    title: str = ""
    event_time: str | None = None
    edited_at: str | None = None
    market: str = ""

    def eligible_for_impact(self) -> bool:
        if not (self.canonical_url and self.published_at and self.content_hash):
            return False
        text = self.raw_text.strip()
        if len(text) < 16:
            return False
        # 列表页场景下标题就是存证原文（如央行/证监会公告标题），
        # 政策标题本身是第一手事实，允许评分；但标题为空的列表页条目不能评分。
        if self.collection_method == "HTML_INCREMENTAL" and len(text) < 200:
            return bool(self.title.strip())
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_document(
    *,
    source_id: str,
    source_tier: str,
    canonical_url: str,
    published_at: str,
    raw_text: str,
    assets: list[str],
    collection_method: str,
    title: str = "",
    language: str = "und",
    market: str = "",
    fetched_at: str | None = None,
    event_time: str | None = None,
    edited_at: str | None = None,
) -> Document:
    fetched = fetched_at or utc_now()
    digest = content_hash(
        canonical_url=canonical_url, published_at=published_at, raw_text=raw_text
    )
    return Document(
        document_id=f"doc-{digest[:16]}",
        source_id=source_id,
        source_tier=source_tier,
        canonical_url=canonical_url,
        published_at=published_at,
        fetched_at=fetched,
        content_hash=digest,
        language=language,
        raw_text=raw_text,
        assets=list(assets),
        collection_method=collection_method,
        title=title,
        event_time=event_time,
        edited_at=edited_at,
        market=market,
    )
