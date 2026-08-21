"""把闭环成交挂上决策账本，并做逐笔审计 + 退出规则重放。

不编造 MAE/MFE。建议停在 SUGGESTION，重放结果只作证据，不能改线上策略。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dsh_runtime.pipeline import evaluate_rule, persist_pipeline, run_optimization_pipeline


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def load_closed_trades(snapshot_dir: str | Path | None, market: str) -> list[dict]:
    raw = snapshot_dir or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or ""
    root = Path(raw)
    name = "CRYPTO.json" if market == "CRYPTO" else "A_SHARE.json"
    path = root / name
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if market == "CRYPTO":
        return [row for row in (payload.get("closed_trades") or []) if row.get("symbol")]
    return [row for row in (payload.get("fills") or []) if row.get("symbol")]


def ingest_and_audit_trades(session, *, market: str, trades: list[dict]) -> dict[str, Any]:
    linked = 0
    imported = 0
    decision_ids: list[str] = []
    for trade in trades:
        fill_id = str(trade.get("source_trade_id") or trade.get("fill_id") or "")
        if not fill_id:
            continue
        signal_id = str(trade.get("signal_id") or "")
        existing = session.ledger.find_by_fill(fill_id)
        if existing is None and signal_id:
            candidate = session.ledger.find_by_signal(signal_id)
            if candidate and not candidate.get("fill_id"):
                existing = candidate
        if existing:
            session.ledger.attach_fill(
                existing["decision_id"],
                fill_id=fill_id,
                order_id=str(trade.get("order_id") or "") or None,
                trade=trade,
            )
            linked += 1
            decision_ids.append(existing["decision_id"])
            continue
        decision_id, _ = session.ledger.upsert(
            {
                "market": market,
                "symbol": trade.get("symbol"),
                "status": "FILLED",
                "intel_grade": "OFFICIAL_PREAUTH",
                "execution_lane": "SHADOW",
                "event_id": fill_id,
                "signal_id": signal_id or None,
                "strategy_id": str(trade.get("signal_type") or "6celue"),
                "strategy_version": str(trade.get("signal_type") or "6celue"),
                "risk_snapshot_id": f"rs-fill-{fill_id}",
                "order_id": trade.get("order_id") or None,
                "fill_id": fill_id,
                "requires_approval": True,
                "action": trade.get("side") or "SELL",
                "direction": "POSITIVE" if _dec(trade.get("pnl")) > 0 else "NEGATIVE",
                "confidence": trade.get("omega_at_entry"),
                "impact_horizon": "1D",
                "entry_plan": {
                    "trigger_price": trade.get("entry_price"),
                    "conditions": ["已成交导入"],
                    "max_capital_ratio": "0.00",
                },
                "exit_plan": {
                    "time_exit": trade.get("closed_at"),
                    "invalidation": [str(trade.get("exit_reason") or "未标注")],
                },
                "evidence_refs": [item for item in (fill_id, signal_id) if item],
                "payload": {"trade": trade, "imported": True, "can_apply": False},
            }
        )
        imported += 1
        decision_ids.append(decision_id)
    for decision_id in decision_ids:
        _ensure_episode(session, market=market, decision_id=decision_id)
    audited = 0
    for row in session.ledger.list(limit=500):
        trade = (row.get("payload") or {}).get("trade")
        if not trade:
            continue
        audit = score_trade(trade)
        session.ledger.attach_audit(row["decision_id"], audit)
        audited += 1
    pipeline = run_optimization_pipeline(trades, market=market)
    now = datetime.now(UTC)
    persist_pipeline(
        session,
        market=market,
        payload=pipeline,
        period_key=now.date().isoformat(),
        created_at=now.isoformat(),
    )
    saved_candidates = _persist_candidates(
        session, market=market, pipeline=pipeline, period_key=now.date().isoformat()
    )
    return {
        "linked": linked,
        "imported": imported,
        "audited": audited,
        "candidates": pipeline["candidates"],
        "candidate_ids": saved_candidates,
        "pipeline": pipeline,
        "coverage": session.ledger.coverage(),
        "mae_mfe": False,
        "can_apply": False,
        "trade_blocked": True,
    }


def _ensure_episode(session, *, market: str, decision_id: str) -> None:
    """每笔已成交决策必须有回合；已平仓的回填退出事实，不编造。"""
    row = session.ledger.get(decision_id)
    if row is None:
        return
    trade = (row.get("payload") or {}).get("trade") or {}
    episode_id = session.ledger.open_episode(
        decision_id,
        market=market,
        symbol=str(row.get("symbol") or ""),
        side=str(row.get("action") or "BUY").upper(),
        entry_fill_id=row.get("fill_id"),
        entry_price=trade.get("entry_price"),
        entry_at=trade.get("opened_at"),
        quantity=str(trade.get("quantity") or "0"),
    )
    episode = session.ledger.get_episode(episode_id)
    if episode and episode["status"] == "OPEN" and trade.get("closed_at"):
        session.ledger.close_episode(
            episode_id,
            exit_fill_id=str(trade.get("source_trade_id") or trade.get("fill_id") or "") or None,
            exit_price=trade.get("exit_price"),
            exit_at=str(trade.get("closed_at")),
            exit_reason=str(trade.get("exit_reason") or "") or None,
            realized_pnl=str(trade.get("pnl") or "") or None,
            fees=str(trade.get("fee") or "0"),
        )


def _persist_candidates(session, *, market: str, pipeline: dict, period_key: str) -> list[str]:
    """优化候选入账本：稳定 candidate_id，重跑不重复；can_apply 恒为 False。"""
    saved: list[str] = []
    for candidate in pipeline.get("candidates") or []:
        candidate_id = f"opt-{market.lower()}-{candidate['suggestion_id']}-{period_key}"
        session.ledger.save_candidate({
            "candidate_id": candidate_id,
            "market": market,
            "title": candidate.get("title"),
            "rule_id": candidate.get("suggestion_id"),
            "reason": candidate.get("reason"),
            "actual_pnl": (candidate.get("replay") or {}).get("actual"),
            "replayed_pnl": (candidate.get("replay") or {}).get("replayed"),
            "delta_pnl": (candidate.get("replay") or {}).get("delta"),
            "backtest": candidate.get("backtest") or {},
            "stage": candidate.get("stage") or "SUGGESTION",
            "next_stage": candidate.get("next_stage"),
            "evidence_refs": candidate.get("evidence") or [],
            "can_apply": False,
        })
        saved.append(candidate_id)
    return saved


def score_trade(trade: dict) -> dict[str, Any]:
    pnl = _dec(trade.get("pnl"))
    fee = _dec(trade.get("fee"))
    pnl_r = _dec(trade.get("pnl_r"))
    hold = _hold_hours(trade.get("opened_at"), trade.get("closed_at"))
    dims = {
        "strategy": {"score": 70 if trade.get("signal_type") else 55, "note": trade.get("signal_type") or "未标注策略版本"},
        "signal": {"score": 80 if pnl > 0 else 35, "note": f"pnl {pnl}"},
        "entry": {"score": 65 if trade.get("omega_at_entry") else 50, "note": f"omega {trade.get('omega_at_entry') or '无'}"},
        "size": {"score": 40 if abs(pnl_r) >= 2 else 70, "note": f"pnl_r {pnl_r}"},
        "execution": {
            "score": max(15, int(100 - min(float(fee / abs(pnl)) if pnl != 0 else 0.2, 0.8) * 100)),
            "note": f"fee {fee}",
        },
        "exit": {
            "score": 70 if pnl > 0 else 45,
            "note": str(trade.get("exit_reason") or "未标注"),
        },
    }
    if hold is not None and hold > 48 and pnl <= 0:
        dims["exit"]["score"] = min(int(dims["exit"]["score"]), 35)
        dims["exit"]["note"] += "；持仓超过 48 小时仍亏损，疑似止损过晚。"
    available = [item["score"] for item in dims.values()]
    return {
        "overall": int(sum(available) / len(available)),
        "dimensions": dims,
        "hold_hours": hold,
        "mae_mfe": False,
        "can_apply": False,
        "pipeline": ["SUGGESTION", "REPLAY", "BACKTEST", "SHADOW", "PAPER", "HUMAN_APPROVAL"],
    }


def replay_exit_candidates(trades: list[dict]) -> list[dict[str, Any]]:
    full = evaluate_rule(trades, "replay-stop-1r")
    if full["n"] < 3 or full["clipped"] == 0 or full["delta"] <= 0:
        return []
    return [
        {
            "suggestion_id": "replay-stop-1r",
            "title": "重放：亏损单在 -1R 离场",
            "reason": "用已有 pnl_r 做历史重放，不是 MAE/MFE，也不是改线上止损。",
            "evidence": [
                f"clipped {full['clipped']}/{full['n']}",
                f"actual_pnl {full['actual']}",
                f"replay_pnl {full['replayed']}",
                f"delta {full['delta']}",
            ],
            "stage": "SUGGESTION",
            "next_stage": "BACKTEST",
            "can_apply": False,
            "trade_blocked": True,
            "pipeline": "建议 → 历史重放 → 回测 → Shadow。暂不执行交易。",
        }
    ]


def _hold_hours(opened: Any, closed: Any) -> float | None:
    try:
        start = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds() / 3600, 2)
