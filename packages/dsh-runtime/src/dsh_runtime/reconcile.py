"""账户对账：用预期值校验，禁止「持仓>0 且权益>0」这种宽松通过。

实际累计成交量 = venue 成交明细汇总（无明细则用 filled_quantity）
预期持仓变化 = 买卖方向 × 实际成交量
预期现金变化 = ±成交金额 − 手续费 − 税费
可用数量 + 冻结数量 = 持仓数量
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


DEFAULT_TOLERANCE = Decimal("0.00000001")


def as_decimal(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def within(actual: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    return abs(actual - expected) <= tolerance


def filled_quantity_from_venue(venue: dict) -> Decimal:
    fills = venue.get("fills") or []
    if fills:
        return sum(
            (as_decimal(item.get("quantity", item.get("qty"))) for item in fills),
            Decimal("0"),
        )
    return as_decimal(venue.get("filled_quantity"))


def expected_position_after(
    baseline_position: Decimal, side: str, filled: Decimal
) -> Decimal:
    delta = filled if str(side).upper() == "BUY" else -filled
    return baseline_position + delta


def expected_cash_after(
    baseline_cash: Decimal,
    side: str,
    filled: Decimal,
    avg_price: Decimal,
    fees: Decimal,
    taxes: Decimal,
) -> Decimal:
    notional = filled * avg_price
    if str(side).upper() == "BUY":
        return baseline_cash - notional - fees - taxes
    return baseline_cash + notional - fees - taxes


@dataclass
class ReconcileVerdict:
    matched: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)


def evaluate_reconcile(
    *,
    side: str,
    baseline_position: object,
    baseline_cash: object,
    venue: dict,
    position: dict | None,
    account: dict | None,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ReconcileVerdict:
    filled = filled_quantity_from_venue(venue)
    avg_price = as_decimal(venue.get("avg_price"))
    fees = as_decimal(venue.get("fees"))
    taxes = as_decimal(venue.get("taxes"))
    base_pos = as_decimal(baseline_position)
    base_cash = as_decimal(baseline_cash)
    exp_pos = expected_position_after(base_pos, side, filled)
    exp_cash = expected_cash_after(base_cash, side, filled, avg_price, fees, taxes)

    details = {
        "side": str(side).upper(),
        "filled_quantity": str(filled),
        "avg_price": str(avg_price),
        "fees": str(fees),
        "taxes": str(taxes),
        "baseline_position": str(base_pos),
        "baseline_cash": str(base_cash),
        "expected_position": str(exp_pos),
        "expected_cash": str(exp_cash),
        "tolerance": str(tolerance),
    }
    reasons: list[str] = []

    if filled <= 0:
        reasons.append(f"filled_quantity {filled} must be > 0")

    if position is None:
        reasons.append("position missing after fill")
        return ReconcileVerdict(False, reasons, details)

    act_pos = as_decimal(position.get("quantity"))
    act_avail = as_decimal(position.get("available_quantity"))
    act_frozen = as_decimal(position.get("frozen_quantity"))
    details.update(
        {
            "actual_position": str(act_pos),
            "available_quantity": str(act_avail),
            "frozen_quantity": str(act_frozen),
        }
    )
    if not within(act_pos, exp_pos, tolerance):
        reasons.append(f"position {act_pos} != expected {exp_pos}")
    if not within(act_avail + act_frozen, act_pos, tolerance):
        reasons.append(
            f"available {act_avail} + frozen {act_frozen} != quantity {act_pos}"
        )

    if account is None:
        reasons.append("account summary missing after fill")
        return ReconcileVerdict(False, reasons, details)

    act_cash = as_decimal(account.get("cash"))
    details["actual_cash"] = str(act_cash)
    details["equity"] = str(account.get("equity", ""))
    details["reconciliation_version"] = str(
        account.get("reconciliation_version") or ""
    )
    if not within(act_cash, exp_cash, tolerance):
        reasons.append(f"cash {act_cash} != expected {exp_cash}")

    frozen_cash = account.get("frozen_cash")
    available_cash = account.get("available_cash")
    if frozen_cash is not None or available_cash is not None:
        avail_c = as_decimal(available_cash, str(act_cash))
        frozen_c = as_decimal(frozen_cash)
        details["available_cash"] = str(avail_c)
        details["frozen_cash"] = str(frozen_c)
        if not within(avail_c + frozen_c, act_cash, tolerance):
            reasons.append(
                f"available_cash {avail_c} + frozen_cash {frozen_c} != cash {act_cash}"
            )

    return ReconcileVerdict(matched=not reasons, reasons=reasons, details=details)
