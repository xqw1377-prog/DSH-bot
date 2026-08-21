"""从本机快照把正式信号记成 Shadow 决策。不预览、不审批、不下单。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dsh_runtime.ledger import build_entry_plan, build_exit_plan, classify_intel
from dsh_runtime.shadow import build_shadow_decision


def load_snapshot_signals(snapshot_dir: str | Path | None, market: str) -> list[dict]:
    raw = snapshot_dir or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or ""
    path = Path(raw) / ("CRYPTO.json" if market == "CRYPTO" else "A_SHARE.json")
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in (payload.get("signals") or []) if row.get("signal_id") and row.get("symbol")]


def record_snapshot_decisions(session, *, market: str, signals: list[dict]) -> dict[str, int]:
    recorded = 0
    skipped = 0
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        if not signal_id:
            continue
        if session.ledger.find_by_signal(signal_id):
            skipped += 1
            continue
        pending = str(signal.get("source_action") or "") == "pending"
        side = str(signal.get("side") or "SELL").upper()
        action = "WATCH" if pending else side
        skip_reason = "WAITING_CONFIRM" if pending else None
        grade, lane, needs_approval = classify_intel(
            authority="official",
            source_tier="PRIMARY",
            action=action,
            held=False,
            title=str(signal.get("symbol") or ""),
            event_type="SIGNAL",
            importance=float(signal.get("strength") or 0.0),
        )
        why = "；".join(str(item) for item in (signal.get("why_source") or []) if item) or "正式信号入账"
        decision = build_shadow_decision(
            signal,
            action=action,
            quantity=str(signal.get("quantity") or "0"),
            suggested_price=signal.get("entry_price"),
            strategy_version=str(signal.get("strategy_version") or ""),
            strength=float(signal.get("strength") or 0.0),
            why=f"{why}。仅作 Shadow 决策。",
            why_not="暂不执行交易，不审批、不下单，不能改线上策略。",
            skip_reason=skip_reason,
            evidence_refs=signal.get("evidence_refs") or [],
            valid_until=signal.get("valid_until"),
        )
        task_id = session.tasks.create(
            kind="shadow-decision",
            subject_id=signal_id,
            payload={**signal, "shadow_decision": decision},
        )
        task = session.tasks.get(task_id)
        if task is not None and task["status"] != "SHADOW_RECORDED":
            session.tasks.transition(task_id, "SHADOW_RECORDED")
        session.ledger.upsert(
            {
                "market": market,
                "symbol": signal.get("symbol"),
                "status": "SHADOW",
                "intel_grade": grade,
                "execution_lane": lane,
                "event_id": None,
                "signal_id": signal_id,
                "strategy_id": signal.get("strategy_id"),
                "strategy_version": signal.get("strategy_version"),
                "risk_snapshot_id": f"rs-{signal_id}",
                "task_id": task_id,
                "requires_approval": needs_approval,
                "action": action,
                "direction": "POSITIVE" if side == "BUY" else "NEGATIVE",
                "confidence": signal.get("strength"),
                "impact_horizon": "1D",
                "entry_plan": build_entry_plan(
                    action=action,
                    price=signal.get("entry_price"),
                    horizon="15m",
                    max_capital_ratio="0.00",
                ),
                "exit_plan": {
                    **build_exit_plan(action=action, horizon="1D", event_type="SIGNAL"),
                    "stop_loss": "-1R",
                    "note": "止损单位是 R，不是编造价格。仅建议，不执行。",
                },
                "evidence_refs": signal.get("evidence_refs") or [],
                "payload": {
                    "can_apply": False,
                    "live_blocked": True,
                    "trade_blocked": True,
                    "mode": "SHADOW",
                    "shadow_decision": decision,
                },
            }
        )
        recorded += 1
    return {"recorded": recorded, "skipped": skipped, "can_apply": False}
