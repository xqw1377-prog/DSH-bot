"""从 ZISU policy_decisions 映射正式 A 股信号。不用 trade/screen 推断。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dsh_snapshot_bridge.symbols import normalize_ashare_symbol
from dsh_snapshot_bridge.timeutil import to_utc_iso, utc_now

_TRADE_ACTIONS = {"buy": "BUY", "add": "BUY", "reduce": "SELL", "exit": "SELL"}


def map_policy_decisions(
    cockpit: dict[str, Any],
    *,
    snapshot_id: str,
    ttl_minutes: int = 30,
    limit: int = 20,
) -> list[dict[str, Any]]:
    policy = cockpit.get("policy_decisions")
    if not isinstance(policy, dict):
        policy = cockpit if isinstance(cockpit.get("decisions"), list) else {}
    if not isinstance(policy, dict):
        return []
    event_id = str(policy.get("_decision_event_id") or "zisu-cycle")
    version = str(policy.get("contract_version") or "zisu-policy")
    generated = (
        to_utc_iso(policy.get("quotes_updated_at"))
        or to_utc_iso((cockpit.get("regime") or {}).get("timestamp"))
        or utc_now().isoformat()
    )
    start = datetime.fromisoformat(generated)
    valid = (start + timedelta(minutes=ttl_minutes)).isoformat()
    mapped: list[dict[str, Any]] = []
    for row in policy.get("decisions") or []:
        if not isinstance(row, dict) or row.get("executable") is not True:
            continue
        action = str(row.get("action") or "").lower()
        side = _TRADE_ACTIONS.get(action)
        if side is None:
            continue
        source = str(row.get("symbol") or "")
        if not source:
            continue
        try:
            symbol, source_symbol = normalize_ashare_symbol(source)
        except ValueError:
            continue
        signal_id = f"{event_id}:{symbol}:{action}"
        leaf = row.get("leaf_signal") if isinstance(row.get("leaf_signal"), dict) else {}
        strength = _strength(leaf.get("confidence"), row.get("evidence_score"), row.get("score"))
        reasons = [str(item) for item in (row.get("reasons") or []) if item]
        mapped.append(
            {
                "signal_id": signal_id,
                "market": "A_SHARE",
                "strategy_id": str(row.get("engine_id") or "zisu-policy"),
                "strategy_version": version,
                "symbol": symbol,
                "source_symbol": source_symbol,
                "side": side,
                "strength": strength,
                "generated_at": generated,
                "valid_until": valid,
                "data_snapshot_id": snapshot_id,
                "quantity": "100",
                "source_action": action,
                "evidence_refs": [
                    f"thesis:{row.get('thesis_id') or ''}",
                    f"decision_event:{event_id}",
                    f"engine:{row.get('engine_id') or 'policy'}",
                ],
                "why_source": reasons,
            }
        )
        if len(mapped) >= limit:
            break
    return mapped


def _strength(*values: Any) -> float:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 1:
            number = number / 100.0
        return max(0.0, min(1.0, number))
    return 0.6
