"""Shadow 决策载荷：只记录、不下单。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

DISCLAIMER = "仅模拟，不会下单"


def _dec(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def build_shadow_decision(
    signal: dict[str, Any],
    *,
    action: str,
    quantity: str,
    suggested_price: str | None,
    strategy_version: str,
    strength: float,
    position_after: str = "0",
    worst_case_loss: str = "0",
    primary_risks: list[str] | None = None,
    why: str,
    why_not: str,
    skip_reason: str | None = None,
    evidence_refs: list[str] | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    price = _dec(suggested_price) if suggested_price not in (None, "") else None
    if price is not None and price > 0:
        low = str((price * Decimal("0.995")).quantize(Decimal("0.0001")))
        high = str((price * Decimal("1.005")).quantize(Decimal("0.0001")))
        mid = str(price)
    else:
        low = high = mid = None
    return {
        "action": action,
        "quantity": quantity,
        "suggested_price": mid,
        "price_low": low,
        "price_high": high,
        "strategy_version": strategy_version,
        "strength": strength,
        "position_after": position_after,
        "worst_case_loss": worst_case_loss,
        "primary_risks": list(primary_risks or []),
        "why": why,
        "why_not": why_not,
        "skip_reason": skip_reason,
        "valid_until": valid_until or signal.get("valid_until"),
        "evidence_refs": list(evidence_refs or signal.get("evidence_refs") or []),
        "simulation_only": True,
        "disclaimer": DISCLAIMER,
        "outcome_price": None,
        "outcome_at": None,
        "simulated_pnl": None,
    }


def simulated_pnl(action: str, suggested_price: Any, outcome_price: Any, quantity: Any) -> str:
    if action not in {"BUY", "SELL"}:
        return "0"
    qty = _dec(quantity)
    suggested = _dec(suggested_price)
    outcome = _dec(outcome_price)
    if qty == 0 or suggested == 0:
        return "0"
    delta = outcome - suggested
    if action == "SELL":
        delta = -delta
    return str(delta * qty)
