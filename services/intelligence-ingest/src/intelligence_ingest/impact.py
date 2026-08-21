"""影响因子。公式固定，不让 LLM 自由打分。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

TIER_SCORE = {"PRIMARY": Decimal("1.0"), "SECONDARY": Decimal("0.6"), "TERTIARY": Decimal("0.3")}
SEVERITY = {
    "EXPLOIT": Decimal("1.0"),
    "CHAIN_HALT": Decimal("0.95"),
    "DEPEG": Decimal("0.9"),
    "DELISTING": Decimal("0.85"),
    "REGULATORY_ACTION": Decimal("0.8"),
    "MONETARY_POLICY": Decimal("0.75"),
    "TRADE_HALT": Decimal("0.7"),
    "TOKEN_UNLOCK": Decimal("0.55"),
    "EARNINGS": Decimal("0.55"),
    "FOUNDER_EXIT": Decimal("0.6"),
    "INDUSTRY_POLICY": Decimal("0.5"),
    "US_MARKET_SPILLOVER": Decimal("0.5"),
    "LISTING": Decimal("0.45"),
    "GOVERNANCE": Decimal("0.4"),
    "REGULATION": Decimal("0.7"),
    "FX_SHOCK": Decimal("0.65"),
    "COMMODITY_SHOCK": Decimal("0.6"),
}


def score_event(event: dict[str, Any], *, held_assets: list[str] | None = None) -> dict[str, Any]:
    held = {item.upper() for item in (held_assets or [])}
    affected = [str(item).upper() for item in event.get("affected_assets") or []]
    relevant = Decimal("1") if affected and any(item in held for item in affected) else Decimal("0.25")
    if not affected:
        relevant = Decimal("0.35")
    first_hand = Decimal("1") if event.get("source_tier") == "PRIMARY" else Decimal("0.4")
    novelty = Decimal("0.8")
    severity = SEVERITY.get(str(event.get("event_type")), Decimal("0.4"))
    priced = Decimal("0")  # 第一版不知道市场是否已反应
    consistency = Decimal("0.5")
    score = (
        Decimal("0.20") * TIER_SCORE.get(str(event.get("source_tier")), Decimal("0.5"))
        + Decimal("0.15") * first_hand
        + Decimal("0.20") * relevant
        + Decimal("0.10") * novelty
        + Decimal("0.15") * severity
        + Decimal("0.10") * consistency
        - Decimal("0.10") * priced
    )
    event = dict(event)
    event["impact_score"] = f"{score:.2f}"
    event["confidence"] = f"{min(Decimal('0.85'), score):.2f}"
    event["direction"] = event.get("direction") or "UNCERTAIN"
    event["mode"] = "SHADOW"
    event["can_apply"] = False
    if relevant >= 1 and severity >= Decimal("0.8"):
        event["max_capital_ratio"] = "0.03"
    else:
        event["max_capital_ratio"] = "0.00"
    event["factors"] = {
        "source_authority": str(TIER_SCORE.get(str(event.get("source_tier")), Decimal("0.5"))),
        "first_hand": str(first_hand),
        "position_relevance": str(relevant),
        "novelty": str(novelty),
        "severity": str(severity),
        "already_priced": str(priced),
        "multi_source": str(consistency),
    }
    return event
