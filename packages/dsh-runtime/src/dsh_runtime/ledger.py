"""决策账本：把情报、决策、订单、成交和审计绑在同一条链上。

Live 不可选。can_apply 永远为 false，除非未来另开门禁。
"""

from __future__ import annotations

from typing import Any

from dsh_contracts import ExecutionLane, IntelGrade

RUMOR_MARKERS = ("传闻", "据说", "unconfirmed", "rumor", "maybe")
PROTECT_EVENTS = {
    "EXPLOIT",
    "CHAIN_HALT",
    "DELISTING",
    "DEPEG",
    "TRADE_HALT",
    "REGULATORY_ACTION",
}


def classify_intel(
    *,
    authority: str | None,
    source_tier: str | None = None,
    action: str,
    held: bool,
    title: str = "",
    event_type: str = "",
    importance: float = 0.0,
) -> tuple[str, str, bool]:
    """返回 (intel_grade, execution_lane, requires_approval)。"""
    text = f"{title} {event_type}".lower()
    if any(marker in text for marker in RUMOR_MARKERS):
        return IntelGrade.OBSERVE.value, ExecutionLane.OBSERVE.value, True
    first_hand = str(authority or "") in {"official", "regulator", "exchange"} or str(
        source_tier or ""
    ).upper() == "PRIMARY"
    if not first_hand:
        if importance >= 0.7:
            return IntelGrade.SECONDARY_CONSENSUS.value, ExecutionLane.SHADOW.value, True
        return IntelGrade.OBSERVE.value, ExecutionLane.OBSERVE.value, True
    if action == "BUY":
        return IntelGrade.RISK_INCREASE.value, ExecutionLane.ADVICE.value, True
    if (action in {"SELL", "HOLD"} and held) or str(event_type).upper() in PROTECT_EVENTS:
        return IntelGrade.RISK_REDUCE.value, ExecutionLane.PROTECT.value, True
    if action == "WATCH" or importance < 0.6:
        return IntelGrade.OFFICIAL_UNCLEAR.value, ExecutionLane.ADVICE.value, True
    return IntelGrade.OFFICIAL_PREAUTH.value, ExecutionLane.SHADOW.value, True


def build_entry_plan(*, action: str, price: Any, horizon: str | None, max_capital_ratio: str) -> dict[str, Any]:
    if action == "BUY":
        conditions = ["等待确认，禁止追涨", "价格触及触发价且未失效"]
    elif action == "SELL":
        conditions = ["进入退出评估", "跌破风险条件才减仓"]
    else:
        conditions = ["观望，不形成即时下单"]
    return {
        "trigger_price": None if price in (None, "") else str(price),
        "conditions": conditions,
        "max_capital_ratio": max_capital_ratio,
        "execute_by": horizon,
    }


def build_exit_plan(*, action: str, horizon: str | None, event_type: str = "") -> dict[str, Any]:
    return {
        "stop_loss": None,
        "take_profit": None,
        "time_exit": horizon or "1D",
        "invalidation": [
            "原文被删除或更正",
            "多来源互相矛盾",
            f"事件类型 {event_type or 'UNKNOWN'} 在复盘中证明无效",
            "超过最晚执行时间仍未触发",
        ],
    }


def lane_allows_shadow(lane: str) -> bool:
    return lane in {
        ExecutionLane.SHADOW.value,
        ExecutionLane.ADVICE.value,
        ExecutionLane.PROTECT.value,
        ExecutionLane.PAPER.value,
    }
