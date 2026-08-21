"""交易质量审计。只读分析，不改策略、不下单。

建议只能停在 SUGGESTION，后续必须走 重放 → 回测 → Shadow → Paper → 人工批准。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

SUGGESTION_PIPELINE = (
    "SUGGESTION",
    "REPLAY",
    "BACKTEST",
    "SHADOW",
    "PAPER",
    "HUMAN_APPROVAL",
)
DISCLAIMER = "只审计，不改正在运行的策略，不会下单。"


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _pnl(row: dict) -> Decimal:
    return _dec(row.get("simulated_pnl")) or Decimal("0")


def _equity(account: dict | None) -> Decimal:
    return _dec((account or {}).get("equity")) or Decimal("0")


def _cash(account: dict | None) -> Decimal:
    return _dec((account or {}).get("cash")) or Decimal("0")


def build_trade_quality_report(
    *,
    decisions: list[dict],
    crypto_account: dict | None = None,
    ashare_account: dict | None = None,
    crypto_positions: list[dict] | None = None,
    ashare_positions: list[dict] | None = None,
    ashare_fills: list[dict] | None = None,
    crypto_fills: list[dict] | None = None,
    closed_trades: list[dict] | None = None,
    equity_curve: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    ledger_coverage: dict | None = None,
    pipeline_candidates: list[dict] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    rows = list(decisions or [])
    ledger = list(ledger_rows or [])
    ledger_fill_rows = [
        row["payload"]["venue_fill"]
        for row in ledger
        if (row.get("payload") or {}).get("venue_fill")
    ]
    ledger_trade_rows = [
        row["payload"]["trade"]
        for row in ledger
        if (row.get("payload") or {}).get("trade")
    ]
    executed = [row for row in rows if row.get("action") in {"BUY", "SELL"}]
    watched = [row for row in rows if row.get("action") == "WATCH" or row.get("skip_reason") == "WAITING_CONFIRM"]
    expired = [row for row in rows if row.get("skip_reason") == "SIGNAL_EXPIRED"]
    closed = [row for row in rows if row.get("skip_reason") == "MARKET_CLOSED"]
    tracked = [
        row
        for row in executed
        if row.get("suggested_price") not in (None, "") and row.get("outcome_price") not in (None, "")
    ]
    fills = [
        row for row in (
            [item for item in ledger_fill_rows if item.get("market") == "A_SHARE"]
            or (ashare_fills or [])
        ) if row.get("symbol")
    ]
    crypto_fill_rows = [
        row for row in (
            [item for item in ledger_fill_rows if item.get("market") == "CRYPTO"]
            or (crypto_fills or [])
        ) if row.get("symbol")
    ]
    trades = [row for row in (ledger_trade_rows or closed_trades or []) if row.get("symbol")]
    curve = [row for row in (equity_curve or []) if row.get("equity") not in (None, "")]
    coverage_meta = ledger_coverage or {}
    coverage = {
        "shadow_decisions": True,
        "suggested_vs_mark": bool(tracked),
        "crypto_fills": bool(crypto_fill_rows or trades or coverage_meta.get("filled")),
        "ashare_fills": bool(fills),
        "fees_slippage": any(row.get("fee") not in (None, "", "0") for row in trades + crypto_fill_rows),
        "mae_mfe": False,
        "exit_reasons": any(row.get("exit_reason") for row in trades),
        "daily_equity_curve": bool(curve),
        "ledger_approved": bool(coverage_meta.get("approved")),
        "ledger_ordered": bool(coverage_meta.get("ordered")),
        "ledger_filled": bool(coverage_meta.get("filled")),
        "ledger_reconciled": bool(coverage_meta.get("reconciled")),
        "ledger_audited": bool(coverage_meta.get("audited")),
    }
    scores = {
        "signal": _score_signal(rows, executed, watched, expired),
        "entry": _score_entry(tracked, trades),
        "exit": _score_exit(trades),
        "size": _score_size(executed, crypto_account, ashare_account),
        "execution": _score_execution(fills, trades),
        "capital": _score_capital(
            crypto_account, ashare_account, crypto_positions, ashare_positions, curve
        ),
    }
    available = [item["score"] for item in scores.values() if item.get("available") and item.get("score") is not None]
    overall = int(sum(available) / len(available)) if available else None
    worst, best = _rank_trades(trades, tracked)
    suggestions = _suggestions(
        rows=rows,
        executed=executed,
        watched=watched,
        expired=expired,
        tracked=tracked,
        crypto_account=crypto_account,
        ashare_account=ashare_account,
        crypto_positions=crypto_positions or [],
        coverage=coverage,
        trades=trades,
    )
    for item in pipeline_candidates or []:
        if item.get("can_apply") is False:
            suggestions.append(item)
    return {
        "as_of": as_of,
        "disclaimer": DISCLAIMER,
        "pipeline": list(SUGGESTION_PIPELINE),
        "trade_blocked": True,
        "coverage": coverage,
        "score": {"overall": overall, "dimensions": scores},
        "counts": {
            "decisions": len(rows),
            "execute": len(executed),
            "watch": len(watched),
            "expired": len(expired),
            "market_closed": len(closed),
            "tracked": len(tracked),
            "ashare_fills": len(fills),
            "crypto_fills": len(crypto_fill_rows),
            "closed_trades": len(trades),
            "ledger_decisions": len(ledger),
        },
        "worst": worst,
        "best": best,
        "exit_notes": scores["exit"].get("notes")
        or [
            "退出质量尚未可评：还没有带退出原因的闭环成交。",
            "仍不能做 MFE/MAE：缺少持仓期内的高低点。",
        ],
        "size_notes": scores["size"].get("notes") or [],
        "suggestions": suggestions,
    }


def _score_signal(rows: list[dict], executed: list[dict], watched: list[dict], expired: list[dict]) -> dict:
    if not rows:
        return {"score": None, "available": False, "note": "还没有 Shadow 决策。"}
    noise = len(watched) + len(expired)
    ratio = noise / len(rows)
    score = max(10, int(100 - ratio * 80))
    if executed:
        score = min(100, score + 8)
    return {
        "score": score,
        "available": True,
        "note": f"正式决策 {len(executed)}，等待确认 {len(watched)}，过期作废 {len(expired)}。",
    }


def _score_entry(tracked: list[dict], trades: list[dict]) -> dict:
    if trades:
        wins = sum(1 for row in trades if (_dec(row.get("pnl")) or Decimal("0")) > 0)
        score = int(100 * wins / len(trades))
        return {
            "score": score,
            "available": True,
            "note": f"闭环成交 {wins}/{len(trades)} 笔盈利（按 6celue pnl_usd）。",
        }
    if not tracked:
        return {"score": None, "available": False, "note": "还没有建议价与后续标记价，无法评进场。"}
    helpful = sum(1 for row in tracked if _pnl(row) > 0)
    score = int(100 * helpful / len(tracked))
    return {
        "score": score,
        "available": True,
        "note": f"{helpful}/{len(tracked)} 笔建议后的标记价朝有利方向。",
    }


def _score_exit(trades: list[dict]) -> dict:
    if not trades:
        return {
            "score": None,
            "available": False,
            "note": "缺少闭环成交与退出原因，还不能做 MFE/MAE。",
            "notes": [
                "退出质量尚未可评：还没有带退出原因的闭环成交。",
                "仍不能做 MFE/MAE：缺少持仓期内的高低点。",
            ],
        }
    reasons: dict[str, int] = {}
    wins = 0
    for row in trades:
        reason = str(row.get("exit_reason") or "未标注")
        reasons[reason] = reasons.get(reason, 0) + 1
        if (_dec(row.get("pnl")) or Decimal("0")) > 0:
            wins += 1
    top = sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:3]
    top_text = "；".join(f"{name} {count}" for name, count in top)
    score = int(100 * wins / len(trades))
    notes = [
        f"已用 6celue 闭环成交的退出原因，仍不能做 MFE/MAE。",
        f"常见退出：{top_text}。" if top_text else "退出原因缺失。",
    ]
    return {"score": score, "available": True, "note": notes[0], "notes": notes}


def _score_size(executed: list[dict], crypto_account: dict | None, ashare_account: dict | None) -> dict:
    notes: list[str] = []
    losses = [_dec(row.get("worst_case_loss")) or Decimal("0") for row in executed]
    equity = _equity(crypto_account) + _equity(ashare_account)
    if not executed:
        return {"score": None, "available": False, "note": "没有可执行 Shadow 单，无法评仓位。", "notes": notes}
    if equity <= 0:
        return {"score": 50, "available": True, "note": "有建议仓位，但账户权益缺失。", "notes": notes}
    worst = max(losses) if losses else Decimal("0")
    share = float(worst / equity) if equity else 0.0
    if share > 0.15:
        score = 35
        notes.append(f"单笔最坏损失约占权益 {share:.1%}，偏重。")
    elif share < 0.005 and executed:
        score = 55
        notes.append("高置信建议的名义仓位很小，可能偏轻。")
    else:
        score = 75
        notes.append(f"单笔最坏损失约占权益 {share:.1%}。")
    return {"score": score, "available": True, "note": notes[0], "notes": notes}


def _score_execution(fills: list[dict], trades: list[dict]) -> dict:
    if trades:
        fees = sum((_dec(row.get("fee")) or Decimal("0")) for row in trades)
        gross = sum(abs(_dec(row.get("pnl")) or Decimal("0")) for row in trades)
        ratio = float(fees / gross) if gross > 0 else 0.0
        score = max(15, int(100 - min(ratio, 0.8) * 100))
        return {
            "score": score,
            "available": True,
            "note": f"{len(trades)} 笔闭环，手续费合计 {fees} USDT，约占绝对盈亏 {ratio:.1%}。",
        }
    if not fills:
        return {
            "score": None,
            "available": False,
            "note": "Crypto 没有成交；A 股 fills 若为空则无法评滑点/手续费。",
        }
    return {
        "score": 60,
        "available": True,
        "note": f"仅有 {len(fills)} 条 A 股纸成交切片，无手续费和滑点字段。",
    }


def _score_capital(
    crypto_account: dict | None,
    ashare_account: dict | None,
    crypto_positions: list[dict] | None,
    ashare_positions: list[dict] | None,
    curve: list[dict] | None = None,
) -> dict:
    crypto_eq = _equity(crypto_account)
    ashare_eq = _equity(ashare_account)
    crypto_cash = _cash(crypto_account)
    ashare_cash = _cash(ashare_account)
    if crypto_eq <= 0 and ashare_eq <= 0:
        return {"score": None, "available": False, "note": "没有账户权益。"}
    idle = 0
    if crypto_eq > 0:
        idle += float(crypto_cash / crypto_eq)
    if ashare_eq > 0:
        idle += float(ashare_cash / ashare_eq)
    markets = int(crypto_eq > 0) + int(ashare_eq > 0)
    idle_avg = idle / markets if markets else 0
    score = max(20, int(100 - idle_avg * 50))
    positions = [row for row in (crypto_positions or []) + (ashare_positions or []) if _dec(row.get("quantity"))]
    note = f"现金占权益约 {idle_avg:.0%}。持仓 {len(positions)} 条。"
    if idle_avg > 0.7:
        note += " 现金偏多，资金效率偏低（观察项，不是下单指令）。"
    if curve and len(curve) >= 2:
        first = _dec(curve[0].get("equity"))
        last = _dec(curve[-1].get("equity"))
        if first and last and first > 0:
            change = float((last - first) / first)
            note += f" 权益曲线 {len(curve)} 点，区间变化 {change:+.1%}。"
    return {"score": score, "available": True, "note": note}


def _rank_trades(trades: list[dict], tracked: list[dict]) -> tuple[list[dict], list[dict]]:
    if trades:
        ranked = sorted(trades, key=lambda row: _dec(row.get("pnl")) or Decimal("0"))
        worst = [
            _closed_card(row, "6celue 闭环亏损")
            for row in ranked[:3]
            if (_dec(row.get("pnl")) or Decimal("0")) < 0
        ]
        best = [
            _closed_card(row, "6celue 闭环盈利")
            for row in list(reversed(ranked))[:3]
            if (_dec(row.get("pnl")) or Decimal("0")) > 0
        ]
        return worst, best
    ranked = sorted(tracked, key=_pnl)
    worst = [_trade_card(row, "亏在建议价之后的标记价") for row in ranked[:3] if _pnl(row) < 0]
    best = [_trade_card(row, "建议后标记价朝有利方向") for row in list(reversed(ranked))[:3] if _pnl(row) > 0]
    return worst, best


def _closed_card(row: dict, reason: str) -> dict[str, Any]:
    return {
        "task_id": row.get("source_trade_id"),
        "market": "CRYPTO",
        "symbol": row.get("symbol"),
        "action": row.get("side"),
        "suggested_price": row.get("entry_price"),
        "outcome_price": row.get("exit_price"),
        "simulated_pnl": row.get("pnl"),
        "skip_reason": row.get("exit_reason"),
        "why": row.get("exit_reason"),
        "reason": reason,
    }


def _trade_card(row: dict, reason: str) -> dict[str, Any]:
    return {
        "task_id": row.get("task_id"),
        "market": row.get("market"),
        "symbol": row.get("symbol"),
        "action": row.get("action"),
        "suggested_price": row.get("suggested_price"),
        "outcome_price": row.get("outcome_price"),
        "simulated_pnl": row.get("simulated_pnl"),
        "strength": row.get("strength"),
        "skip_reason": row.get("skip_reason"),
        "why": row.get("why"),
        "reason": reason,
    }


def _suggestions(
    *,
    rows: list[dict],
    executed: list[dict],
    watched: list[dict],
    expired: list[dict],
    tracked: list[dict],
    crypto_account: dict | None,
    ashare_account: dict | None,
    crypto_positions: list[dict],
    coverage: dict,
    trades: list[dict] | None = None,
) -> list[dict[str, Any]]:
    trades = trades or []
    items: list[dict[str, Any]] = []

    def add(key: str, title: str, reason: str, evidence: list[str]) -> None:
        items.append(
            {
                "suggestion_id": key,
                "title": title,
                "reason": reason,
                "evidence": evidence,
                "stage": "SUGGESTION",
                "next_stage": "REPLAY",
                "can_apply": False,
                "pipeline": "建议 → 历史重放 → 回测 → Shadow → Paper → 人工批准",
            }
        )

    if watched and len(watched) / max(len(rows), 1) >= 0.2:
        add(
            "watch-not-sell",
            "把等待确认从可卖清单拿掉",
            "大量正式信号仍在等确认 K 线。继续把它们记成 SELL/过期，会污染交易质量。",
            [f"WATCH/WAITING_CONFIRM {len(watched)} 条"],
        )
    if expired and len(expired) >= 5:
        add(
            "ttl-vs-confirm",
            "确认单不要用 15 分钟 TTL 作废",
            "6celue pending 确认单存活远长于 15 分钟。用短 TTL 会把有效观察记成 SIGNAL_EXPIRED。",
            [f"SIGNAL_EXPIRED {len(expired)} 条"],
        )
    if executed and not tracked:
        add(
            "need-mark-followup",
            "给已执行 Shadow 单补后续价",
            "有买入/卖出建议但没有 outcome_price，进场质量评不了。",
            [f"execute {len(executed)}，tracked 0"],
        )
    crypto_eq = _equity(crypto_account)
    crypto_cash = _cash(crypto_account)
    if crypto_eq > 0 and crypto_cash / crypto_eq > Decimal("0.8"):
        add(
            "crypto-idle-cash",
            "观察 Crypto 现金占比是否过高",
            "现金占权益过高。这是资金效率观察，不是现在去买的指令。",
            [f"cash {crypto_cash} / equity {crypto_eq}"],
        )
    if not coverage["crypto_fills"]:
        add(
            "need-crypto-fills",
            "接入 6celue 成交与手续费（只读）",
            "没有 Crypto 委托/成交，执行质量和真实退出都评不了。不要从持仓反推成交。",
            ["crypto_fills=false", "fees_slippage=false"],
        )
    if coverage["ledger_ordered"] and not coverage["ledger_reconciled"]:
        add(
            "ledger-reconcile-gap",
            "把已下单链路补到对账完成",
            "账本里已经有审批/下单，但还没有稳定进入 MATCHED。审计应优先消费完整账本关系，而不是长期靠快照补洞。",
            [
                f"ledger_ordered={coverage['ledger_ordered']}",
                f"ledger_reconciled={coverage['ledger_reconciled']}",
            ],
        )
    elif coverage["fees_slippage"] and trades:
        fees = sum((_dec(row.get("fee")) or Decimal("0")) for row in trades)
        if fees > 0:
            add(
                "review-fees",
                "复核手续费是否侵蚀短线收益",
                "已读到 6celue fee_usd。先重放高换手品种，不要据此改线上参数。",
                [f"closed_trades {len(trades)}", f"fees {fees}"],
            )
    if crypto_positions and len(crypto_positions) <= 2 and crypto_eq > 0:
        add(
            "concentration",
            "检查币持仓集中度",
            "持仓条数很少，相关性和回撤会一起放大。先统计，不调仓。",
            [f"crypto positions {len(crypto_positions)}"],
        )
    if not items:
        add(
            "keep-shadow-only",
            "继续只审计、不改策略",
            "样本还不够支撑优化建议。保持 Shadow，禁止把评分写成改参指令。",
            [f"decisions {len(rows)}"],
        )
    return items
