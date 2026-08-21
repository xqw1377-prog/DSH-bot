"""优化管线：建议 → 重放 → 回测 → Shadow。到此停止。

只用已有成交字段。不编造 MAE/MFE。不改线上策略。不下单。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from collections.abc import Callable

PIPELINE = ("SUGGESTION", "REPLAY", "BACKTEST", "SHADOW")
BLOCKED_STAGES = ("PAPER", "HUMAN_APPROVAL", "LIVE")
TRADE_BLOCK_REASON = "暂不执行交易。Shadow 只记账，不发单、不改 6celue/ZISU。"


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _hold_hours(trade: dict) -> float | None:
    try:
        start = datetime.fromisoformat(str(trade.get("opened_at")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(trade.get("closed_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (end - start).total_seconds() / 3600


def _sorted(trades: list[dict]) -> list[dict]:
    return sorted(
        trades,
        key=lambda row: str(row.get("closed_at") or row.get("opened_at") or ""),
    )


def _apply_stop_r(trades: list[dict], cap: Decimal) -> dict[str, Any]:
    usable = [row for row in trades if row.get("pnl_r") not in (None, "")]
    actual = sum((_dec(row.get("pnl")) for row in usable), Decimal("0"))
    replayed = Decimal("0")
    clipped = 0
    for row in usable:
        pnl = _dec(row.get("pnl"))
        pnl_r = _dec(row.get("pnl_r"))
        if pnl_r < cap and pnl_r != 0:
            replayed += pnl * (cap / pnl_r)
            clipped += 1
        else:
            replayed += pnl
    return {
        "n": len(usable),
        "clipped": clipped,
        "actual": actual,
        "replayed": replayed,
        "delta": replayed - actual,
    }


def _apply_skip_hold_losers(trades: list[dict]) -> dict[str, Any]:
    actual = sum((_dec(row.get("pnl")) for row in trades), Decimal("0"))
    replayed = Decimal("0")
    clipped = 0
    for row in trades:
        pnl = _dec(row.get("pnl"))
        hold = _hold_hours(row)
        if hold is not None and hold > 48 and pnl <= 0:
            clipped += 1
            continue
        replayed += pnl
    return {
        "n": len(trades),
        "clipped": clipped,
        "actual": actual,
        "replayed": replayed,
        "delta": replayed - actual,
    }


def _apply_skip_fee_drag(trades: list[dict]) -> dict[str, Any]:
    usable = [row for row in trades if row.get("pnl") not in (None, "")]
    actual = sum((_dec(row.get("pnl")) for row in usable), Decimal("0"))
    replayed = Decimal("0")
    clipped = 0
    for row in usable:
        pnl = _dec(row.get("pnl"))
        fee = _dec(row.get("fee"))
        if pnl != 0 and fee >= abs(pnl) * Decimal("0.3"):
            clipped += 1
            continue
        replayed += pnl
    return {
        "n": len(usable),
        "clipped": clipped,
        "actual": actual,
        "replayed": replayed,
        "delta": replayed - actual,
    }


RULES: dict[str, tuple[str, Callable[[list[dict]], dict[str, Any]]]] = {
    "replay-stop-1r": ("亏损单在 -1R 离场", lambda rows: _apply_stop_r(rows, Decimal("-1"))),
    "replay-stop-2r": ("亏损单在 -2R 离场", lambda rows: _apply_stop_r(rows, Decimal("-2"))),
    "replay-skip-hold-48h": ("排除持仓超过 48 小时的亏损单", _apply_skip_hold_losers),
    "replay-skip-fee-drag": ("排除手续费吃掉 30% 以上盈亏的单", _apply_skip_fee_drag),
}


def evaluate_rule(trades: list[dict], rule_id: str) -> dict[str, Any]:
    title, apply = RULES[rule_id]
    stats = apply(_sorted(trades))
    return {"rule_id": rule_id, "title": title, **stats}


def walk_forward(trades: list[dict], rule_id: str) -> dict[str, Any]:
    rows = _sorted(trades)
    if rule_id.startswith("replay-stop-"):
        rows = [row for row in rows if row.get("pnl_r") not in (None, "")]
    if len(rows) < 6:
        return {"ok": False, "reason": "样本不足 6 笔，不能做样本外回测", "n_train": 0, "n_test": 0}
    cut = max(3, int(len(rows) * 0.7))
    if len(rows) - cut < 2:
        return {"ok": False, "reason": "测试集不足", "n_train": cut, "n_test": len(rows) - cut}
    train = evaluate_rule(rows[:cut], rule_id)
    test = evaluate_rule(rows[cut:], rule_id)
    return {
        "ok": True,
        "n_train": train["n"],
        "n_test": test["n"],
        "train_delta": train["delta"],
        "test_delta": test["delta"],
        "train_actual": train["actual"],
        "test_actual": test["actual"],
        "train_replayed": train["replayed"],
        "test_replayed": test["replayed"],
    }


def _stage_for(full: dict[str, Any], backtest: dict[str, Any]) -> tuple[str, str]:
    if full["clipped"] <= 0 or full["delta"] <= 0:
        return "SUGGESTION", "REPLAY"
    if not backtest.get("ok"):
        return "REPLAY", "BACKTEST"
    if backtest["test_delta"] > 0:
        return "SHADOW", "PAPER"
    return "BACKTEST", "SHADOW"


def run_optimization_pipeline(trades: list[dict], *, market: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for rule_id in RULES:
        full = evaluate_rule(trades, rule_id)
        if full["n"] < 3:
            continue
        backtest = walk_forward(trades, rule_id)
        stage, next_stage = _stage_for(full, backtest)
        blocked = next_stage in BLOCKED_STAGES or stage == "SHADOW"
        candidates.append(
            {
                "suggestion_id": rule_id,
                "title": full["title"],
                "reason": "只用已有 pnl / pnl_r / 持仓时间 / 手续费重放。不是 MAE/MFE，也不改线上参数。",
                "evidence": [
                    f"clipped {full['clipped']}/{full['n']}",
                    f"actual_pnl {full['actual']}",
                    f"replay_pnl {full['replayed']}",
                    f"delta {full['delta']}",
                    f"backtest_ok {backtest.get('ok')}",
                    f"test_delta {backtest.get('test_delta', '')}",
                ],
                "stage": stage,
                "next_stage": next_stage,
                "pipeline": list(PIPELINE),
                "can_apply": False,
                "trade_blocked": True,
                "block_reason": TRADE_BLOCK_REASON if blocked else None,
                "mae_mfe": False,
                "market": market,
                "replay": {
                    "n": full["n"],
                    "clipped": full["clipped"],
                    "actual": str(full["actual"]),
                    "replayed": str(full["replayed"]),
                    "delta": str(full["delta"]),
                },
                "backtest": {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in backtest.items()
                },
            }
        )
    return {
        "market": market,
        "candidates": candidates,
        "shadowed": [row for row in candidates if row["stage"] == "SHADOW"],
        "can_apply": False,
        "trade_blocked": True,
        "mae_mfe": False,
        "pipeline": list(PIPELINE),
        "blocked_after": "SHADOW",
    }


def persist_pipeline(session, *, market: str, payload: dict, period_key: str, created_at: str) -> str:
    return session.reports.upsert(
        report_kind="optimization-daily",
        period_key=period_key,
        market=market,
        payload={**payload, "can_apply": False, "mae_mfe": False},
        created_at=created_at,
    )
