"""把原文抽成结构化事件。第一版用确定性关键词，不把评分交给 LLM。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from intelligence_ingest.documents import Document

CRYPTO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EXPLOIT", ("exploit", "hack", "hacked", "漏洞", "被盗", "attack")),
    ("CHAIN_HALT", ("halt", "outage", "downtime", "宕机", "停链", "reorg")),
    ("DELISTING", ("delist", "下架", "下币")),
    ("LISTING", ("listing", "上币", "listed")),
    ("TOKEN_UNLOCK", ("unlock", "解锁")),
    ("DEPEG", ("depeg", "脱锚", "lost peg")),
    ("FOUNDER_EXIT", ("resign", "离职", "step down", "founder exit")),
    ("REGULATION", ("sec ", "lawsuit", "监管", "处罚", "ban")),
    ("GOVERNANCE", ("release", "upgrade", "governance", "proposal", "升级", "提案")),
)

ASHARE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MONETARY_POLICY", ("降准", "降息", "公开市场", "逆回购", "lpr", "利率")),
    ("REGULATORY_ACTION", ("处罚", "问询", "立案", "警示", "监管")),
    ("EARNINGS", ("业绩预告", "业绩修正", "年报", "半年报")),
    ("TRADE_HALT", ("停牌", "复牌", "halt")),
    ("INDUSTRY_POLICY", ("产业", "补贴", "限制", "规划", "政策")),
    ("FX_SHOCK", ("汇率", "外汇", "人民币")),
    ("US_MARKET_SPILLOVER", ("美股", "纳斯达克", "标普", "8-k", "form 4")),
)

US_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TRADE_HALT", ("halt", "resume", "paused")),
    ("EARNINGS", ("10-k", "10-q", "8-k", "form 4", "form 3", "form 5", "earnings", "filed:")),
    ("REGULATORY_ACTION", ("enforcement", "wells", "penalty")),
    ("US_MARKET_SPILLOVER", ("nasdaq", "nyse", "dow", "s&p")),
)


def extract_event(doc: Document) -> dict[str, Any] | None:
    if not doc.eligible_for_impact():
        return None
    text = f"{doc.title}\n{doc.raw_text}\n{doc.canonical_url}".lower()
    rules = CRYPTO_RULES if doc.market == "CRYPTO" else US_RULES if doc.market == "US" else ASHARE_RULES
    event_type = None
    for name, keywords in rules:
        if any(keyword in text for keyword in keywords):
            event_type = name
            break
    if event_type is None:
        return None
    digest = sha256(f"{doc.document_id}:{event_type}".encode()).hexdigest()[:16]
    return {
        "event_id": f"evt-{digest}",
        "document_id": doc.document_id,
        "event_type": event_type,
        "affected_assets": list(doc.assets),
        "direction": "UNCERTAIN",
        "confidence": "0.40",
        "impact_horizon": "1D",
        "entry_conditions": [],
        "exit_conditions": [],
        "invalidation_conditions": [],
        "max_capital_ratio": "0.00",
        "evidence_refs": [doc.document_id],
        "mode": "SHADOW",
        "can_apply": False,
        "source_tier": doc.source_tier,
        "title": doc.title,
        "canonical_url": doc.canonical_url,
        "published_at": doc.published_at,
        "market": doc.market,
    }
