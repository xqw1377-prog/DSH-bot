"""从 6celue state.json 映射 CRYPTO 快照。不读 Streamlit，不读密钥。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from dsh_snapshot_bridge.decimalutil import decimal_string
from dsh_snapshot_bridge.schema import SCHEMA_VERSION
from dsh_snapshot_bridge.symbols import normalize_crypto_symbol
from dsh_snapshot_bridge.timeutil import require_utc_iso, to_utc_iso, utc_now


def load_crypto_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"CRYPTO_STATE_MISSING: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"CRYPTO_STATE_INVALID_JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("CRYPTO_STATE_INVALID_JSON: root must be an object")
    return payload


def map_crypto_state(
    state: dict[str, Any],
    *,
    account_id: str,
    source_system: str,
    source_mode: str,
    stale_after_seconds: int,
    signals: list[dict[str, Any]] | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
    closed_trades: list[dict[str, Any]] | None = None,
    equity_curve: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed = require_utc_iso(state.get("last_update"), field="last_update")
    exported = utc_now().isoformat()
    age = _age_seconds(state.get("last_update"), exported)
    balance = state.get("balance")
    if not isinstance(balance, dict):
        raise ValueError("CRYPTO_STATE_INCOMPLETE: balance missing")

    sync_ok = balance.get("sync_ok")
    stale = age is not None and age > stale_after_seconds
    fresh = (sync_ok is not False) and not stale
    detail_parts = [f"source={source_system}", f"mode={source_mode}"]
    if sync_ok is False:
        detail_parts.append("error=SOURCE_SYNC_ERROR")
    if stale:
        detail_parts.append("error=SOURCE_STALE")
    if not fresh:
        detail_parts.append("data_fresh=false")

    positions = _map_positions(state.get("exchange_positions") or {}, account_id)
    cash = decimal_string(balance.get("available_balance"), field="available_balance")
    equity = decimal_string(balance.get("total_balance"), field="total_balance")
    margin = None
    if balance.get("margin_used") is not None:
        margin = decimal_string(balance.get("margin_used"), field="margin_used")

    detail = "; ".join(detail_parts)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": str(uuid4()),
        "market": "CRYPTO",
        "account_id": account_id,
        "source_system": source_system,
        "source_mode": source_mode,
        "source_observed_at": observed,
        "exported_at": exported,
        "data_fresh": fresh,
        "degraded": not fresh,
        "detail": detail,
        "last_success_exported_at": exported if fresh else None,
        "last_error_type": None if fresh else ("SOURCE_STALE" if stale else "SOURCE_SYNC_ERROR"),
        "last_error_at": None if fresh else exported,
        "health": {
            "system_ok": True,
            "data_fresh": fresh,
            "trading_channel_ok": False,
            "clock_skew_ms": int(age * 1000) if age is not None else 0,
            "degraded": not fresh,
            "detail": detail,
            "market_session": "OPEN",
        },
        "accounts": [
            {
                "account_id": account_id,
                "cash": cash,
                "equity": equity,
                "available_cash": cash,
                "frozen_cash": "0",
                "margin_used": margin,
                "currency": "USDT",
                "reconciliation_version": "6celue-state-v1",
                "source_observed_at": observed,
            }
        ],
        "positions": positions,
        "orders": [],
        "signals": list(signals or []),
        "rejected_candidates": list(rejected_candidates or []),
        "screen_results": [],
        "fills": list(fills or []),
        "closed_trades": list(closed_trades or []),
        "equity_curve": list(equity_curve or []),
        "fee_rate": state.get("fee_rate"),
        "slippage": state.get("slippage"),
    }


def _map_positions(raw: Any, account_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("CRYPTO_STATE_INCOMPLETE: exchange_positions must be an object")
    rows: list[dict[str, Any]] = []
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        source_symbol = str(item.get("symbol") or key)
        symbol, source_symbol = normalize_crypto_symbol(source_symbol)
        side = str(item.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        qty = decimal_string(item.get("quantity"), field=f"{source_symbol}.quantity")
        if side == "SELL" and not qty.startswith("-"):
            qty = f"-{qty}" if qty != "0" else qty
        cost = item.get("entry_price")
        if cost is None:
            raise ValueError(f"CRYPTO_STATE_INCOMPLETE: {source_symbol} missing entry_price")
        rows.append(
            {
                "account_id": account_id,
                "symbol": symbol,
                "source_symbol": source_symbol,
                "quantity": qty,
                "available_quantity": qty,
                "frozen_quantity": "0",
                "avg_cost": decimal_string(cost, field=f"{source_symbol}.entry_price"),
                "currency": "USDT",
                "source_side": side,
            }
        )
    return rows


def _age_seconds(raw_ts: Any, exported_iso: str) -> float | None:
    observed = to_utc_iso(raw_ts)
    if observed is None:
        return None
    from datetime import datetime

    exported = datetime.fromisoformat(exported_iso)
    observed_dt = datetime.fromisoformat(observed)
    return max(0.0, (exported - observed_dt).total_seconds())
