"""从 6celue signals.jsonl 映射正式 CRYPTO 信号。不从持仓或选币推断。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dsh_snapshot_bridge.symbols import normalize_crypto_symbol
from dsh_snapshot_bridge.timeutil import to_utc_iso, utc_now

_TIER_STRENGTH = {"S": 0.9, "A": 0.7, "B": 0.5}
_KEEP_ACTIONS = {"pending", "executed"}
_SKIP_TYPES = {"V5_SYNC_ADOPT", "EVENT_EXECUTOR"}


def default_signals_path(state_path: Path) -> Path:
    return state_path.parent / "signals.jsonl"


def load_jsonl_tail(path: Path, *, max_lines: int = 2500) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows: list[dict[str, Any]] = []
    for line in raw[-max_lines:]:
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def map_crypto_signals(
    rows: list[dict[str, Any]],
    *,
    strategy_version: str,
    snapshot_id: str,
    ttl_minutes: int = 15,
    limit: int = 20,
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):
        if str(row.get("action") or "") not in _KEEP_ACTIONS:
            continue
        if str(row.get("signal_type") or "") in _SKIP_TYPES:
            continue
        side_raw = str(row.get("side") or "").upper()
        if side_raw not in {"LONG", "SHORT"}:
            continue
        source_symbol = str(row.get("symbol") or "")
        if not source_symbol:
            continue
        symbol, source_symbol = normalize_crypto_symbol(source_symbol)
        generated = to_utc_iso(row.get("timestamp")) or utc_now().isoformat()
        signal_id = str(
            row.get("client_order_id")
            or f"{symbol}:{row.get('timestamp')}:{side_raw}:{row.get('action')}"
        )
        if not str(row.get("client_order_id") or "").strip():
            signal_id = f"{symbol}:{row.get('timestamp')}:{side_raw}:{row.get('action')}"
        if signal_id in seen:
            continue
        seen.add(signal_id)
        start = datetime.fromisoformat(generated)
        valid = (start + timedelta(minutes=ttl_minutes)).isoformat()
        tier = str(row.get("signal_tier") or "B").upper()
        strength = _TIER_STRENGTH.get(tier, 0.5)
        omega = row.get("omega")
        if omega is not None:
            try:
                strength = max(strength, min(1.0, abs(float(omega))))
            except (TypeError, ValueError):
                pass
        reason = str(row.get("reject_reason") or "").strip()
        mapped.append(
            {
                "signal_id": signal_id,
                "market": "CRYPTO",
                "strategy_id": "6celue-v5",
                "strategy_version": strategy_version,
                "symbol": symbol,
                "source_symbol": source_symbol,
                "side": "BUY" if side_raw == "LONG" else "SELL",
                "strength": strength,
                "generated_at": generated,
                "valid_until": valid,
                "data_snapshot_id": snapshot_id,
                "quantity": "0.01",
                "entry_price": (
                    None if row.get("entry_price") in (None, "") else str(row.get("entry_price"))
                ),
                "source_action": str(row.get("action")),
                "why_source": [reason] if reason else [],
                "evidence_refs": [
                    f"6celue:signals.jsonl:{signal_id}",
                    f"6celue:signal_type:{row.get('signal_type') or 'V5'}",
                    f"6celue:tier:{tier}",
                    *( [f"6celue:note:{reason}"] if reason else [] ),
                ],
            }
        )
        if len(mapped) >= limit:
            break
    return mapped


def map_crypto_rejects(rows: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    """最近被 6celue 自己挡掉的候选。不是正式信号，禁止进入 signals。"""
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):
        action = str(row.get("action") or "")
        if action not in {"filtered", "rejected", "cooldown"}:
            continue
        source = str(row.get("symbol") or "")
        if not source:
            continue
        symbol, source_symbol = normalize_crypto_symbol(source)
        key = f"{symbol}:{row.get('reject_reason') or action}"
        if key in seen:
            continue
        seen.add(key)
        mapped.append(
            {
                "kind": "REJECTED_CANDIDATE",
                "symbol": symbol,
                "source_symbol": source_symbol,
                "action": action,
                "reason": str(row.get("reject_reason") or action),
                "tier": str(row.get("signal_tier") or ""),
                "entry_price": (
                    None if row.get("entry_price") in (None, "") else str(row.get("entry_price"))
                ),
            }
        )
        if len(mapped) >= limit:
            break
    return mapped
