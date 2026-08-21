"""把闭环成交挂上决策账本，并做逐笔审计 + 退出规则重放。

不编造 MAE/MFE。建议停在 SUGGESTION，重放结果只作证据，不能改线上策略。
"""

from __future__ import annotations

import httpx
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
    warning_rows: list[dict[str, Any]] = []
    for row in session.ledger.list(limit=500):
        trade = (row.get("payload") or {}).get("trade")
        if not trade:
            continue
        audit = score_trade(trade)
        session.ledger.attach_audit(row["decision_id"], audit)
        audited += 1
        if audit.get("warnings"):
            warning_rows.append({
                "decision_id": row["decision_id"],
                "symbol": row.get("symbol"),
                "fill_id": row.get("fill_id"),
                "warnings": audit["warnings"],
            })
    pipeline = run_optimization_pipeline(trades, market=market)
    now = datetime.now(UTC)
    persist_pipeline(
        session,
        market=market,
        payload={**pipeline, "warnings": warning_rows},
        period_key=now.date().isoformat(),
        created_at=now.isoformat(),
    )
    build_weekly_summary(session, market=market, today=now)
    saved_candidates = _persist_candidates(
        session, market=market, pipeline=pipeline, period_key=now.date().isoformat()
    )
    streak = _losing_streak(session)
    if streak >= 3:
        warning_rows.insert(0, {
            "decision_id": None,
            "symbol": None,
            "fill_id": None,
            "warnings": [f"连续亏损 {streak} 笔,建议人工复核近端交易"],
        })
    return {
        "linked": linked,
        "imported": imported,
        "audited": audited,
        "warnings": warning_rows,
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
    audit = {
        "overall": int(sum(available) / len(available)),
        "dimensions": dims,
        "hold_hours": hold,
        "mae_mfe": False,
        "can_apply": False,
        "pipeline": ["SUGGESTION", "REPLAY", "BACKTEST", "SHADOW", "PAPER", "HUMAN_APPROVAL"],
    }
    audit["warnings"] = deviation_warnings(trade, audit)
    return audit


def deviation_warnings(trade: dict, audit: dict | None = None) -> list[str]:
    """实时交易偏差警告：只陈述可从已有字段核实的事实，不推测。

    每笔已审计成交都可能产生警告；警告进入审计附件与每日汇总，
    供今日看板与交易质量报告展示。
    """
    warnings: list[str] = []
    pnl = _dec(trade.get("pnl"))
    fee = _dec(trade.get("fee"))
    pnl_r = _dec(trade.get("pnl_r"))
    hold = _hold_hours(trade.get("opened_at"), trade.get("closed_at"))
    if pnl != 0 and fee >= abs(pnl) * Decimal("0.3"):
        warnings.append(f"手续费拖累: fee {fee} >= 30% |pnl| {abs(pnl)}")
    if hold is not None and hold > 48 and pnl <= 0:
        warnings.append(f"止损过晚嫌疑: 持仓 {hold:.1f}h 仍亏损 {pnl}")
    if pnl_r <= Decimal("-2"):
        warnings.append(f"单笔亏损超 2R: pnl_r {pnl_r}")
    if audit is not None:
        exit_score = ((audit.get("dimensions") or {}).get("exit") or {}).get("score")
        if isinstance(exit_score, int) and exit_score < 40:
            warnings.append(f"退出质量低分: {exit_score}/100")
    return warnings


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


def _losing_streak(session, limit: int = 50) -> int:
    """从最近已平仓决策倒数的连续亏损笔数。"""
    streak = 0
    for row in session.ledger.list(limit=limit):
        trade = (row.get("payload") or {}).get("trade")
        if not trade or not trade.get("closed_at"):
            continue
        if _dec(trade.get("pnl")) < 0:
            streak += 1
        else:
            break
    return streak


def build_weekly_summary(session, *, market: str, today=None) -> dict[str, Any]:
    """每周策略优化候选汇总:规则跨日稳定性 + 阶段推进轨迹。"""
    from datetime import timedelta

    now = today or datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday())).date().isoformat()
    dailies: list[dict] = []
    for offset in range(7):
        key = (now - timedelta(days=offset)).date().isoformat()
        for row in session.reports.list(report_kind="optimization-daily", limit=200):
            if row.get("period_key") == key and row.get("market") == market:
                dailies.append({"period_key": key, "payload": row.get("payload") or {}})
                break
    rules: dict[str, dict] = {}
    for daily in dailies:
        for cand in (daily["payload"].get("candidates") or []):
            rule = cand.get("suggestion_id")
            if not rule:
                continue
            entry = rules.setdefault(rule, {
                "suggestion_id": rule,
                "title": cand.get("title"),
                "days_seen": 0,
                "max_stage": "SUGGESTION",
                "deltas": [],
                "latest_period": None,
            })
            entry["days_seen"] += 1
            entry["latest_period"] = daily["period_key"]
            replay = cand.get("replay") or {}
            if replay.get("delta") not in (None, ""):
                entry["deltas"].append(str(replay["delta"]))
            order = ["SUGGESTION", "REPLAY", "BACKTEST", "SHADOW"]
            if (cand.get("stage") in order
                    and order.index(cand["stage"]) > order.index(entry["max_stage"])):
                entry["max_stage"] = cand["stage"]
    weekly = {
        "market": market,
        "week_of": monday,
        "days_covered": len({d["period_key"] for d in dailies}),
        "rules": sorted(rules.values(), key=lambda r: -r["days_seen"]),
        "can_apply": False,
        "trade_blocked": True,
        "mae_mfe": False,
    }
    session.reports.upsert(
        report_kind="optimization-weekly",
        period_key=monday,
        market=market,
        payload=weekly,
        created_at=now.isoformat(),
    )
    return weekly


def nominate_candidate_to_evolution(session, *, candidate_id: str,
                                    evolution_url: str,
                                    api_key: str | None = None) -> dict[str, Any]:
    """把 Shadow 阶段的优化候选提名到 strategy-evolution 正式状态机。

    只创建 DRAFT 候选(注册),不晋级——晋级必须走 evolution 的证据门禁
    + 网关审批回查 + 独立审计。幂等:已提名过直接返回。
    """
    row = session.ledger.get_candidate(candidate_id)
    if row is None:
        raise KeyError(f"candidate not found: {candidate_id}")
    # get_candidate 返回扁平化行(payload 字段已展开进行)
    if row.get("nominated_evolution_id"):
        return {
            "candidate_id": candidate_id,
            "evolution_candidate_id": row["nominated_evolution_id"],
            "already_nominated": True,
        }
    if row.get("stage") != "SHADOW":
        raise ValueError(
            f"only SHADOW-stage candidates may be nominated (stage={row.get('stage')})")
    headers = {"X-API-Key": api_key} if api_key else None
    body = {
        "market": row.get("market"),
        "strategy_id": str(row.get("rule_id") or candidate_id),
        "strategy_version": "optimized-1",
    }
    resp = httpx.post(
        f"{evolution_url.rstrip('/')}/v1/candidates",
        json=body, headers=headers, timeout=5.0,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"evolution nomination failed ({resp.status_code}): {resp.text[:200]}")
    evolution_id = resp.json().get("candidate_id")
    updated = {**row, "nominated_evolution_id": evolution_id}
    session.ledger.save_candidate({
        key: value for key, value in updated.items()
        if key not in ("created_at", "updated_at", "next_stage")
    })
    return {
        "candidate_id": candidate_id,
        "evolution_candidate_id": evolution_id,
        "already_nominated": False,
    }
