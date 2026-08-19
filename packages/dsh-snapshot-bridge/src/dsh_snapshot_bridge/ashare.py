"""从 ZISU /api/paper/wallet 与 /api/trade/screen 映射 A_SHARE 快照。

API 不可用时失败关闭，不读 zisu.db。trade/screen 只作为 SCREEN_RESULT。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from dsh_snapshot_bridge.decimalutil import decimal_string
from dsh_snapshot_bridge.schema import SCHEMA_VERSION
from dsh_snapshot_bridge.symbols import assert_no_symbol_collisions, normalize_ashare_symbol
from dsh_snapshot_bridge.timeutil import require_utc_iso, utc_now

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class AShareSourceError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def fetch_json(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise AShareSourceError("SOURCE_TIMEOUT", f"A-share API timeout: {url}") from exc
    except httpx.HTTPError as exc:
        raise AShareSourceError("SOURCE_UNAVAILABLE", f"A-share API unavailable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise AShareSourceError("SOURCE_INVALID_JSON", "A-share API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AShareSourceError("SOURCE_INVALID_JSON", "A-share API root must be an object")
    return payload


def a_share_session(now: datetime | None = None) -> str:
    current = now or datetime.now(_SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI)
    else:
        current = current.astimezone(_SHANGHAI)
    if current.weekday() >= 5:
        return "CLOSED"
    clock = current.time()
    from datetime import time

    morning = time(9, 30) <= clock < time(11, 30)
    afternoon = time(13, 0) <= clock < time(15, 0)
    return "OPEN" if morning or afternoon else "CLOSED"


def map_ashare_payloads(
    wallet: dict[str, Any],
    screen: dict[str, Any],
    *,
    account_id: str,
    source_system: str,
    source_mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed = require_utc_iso(wallet.get("updated_at") or utc_now(), field="updated_at")
    exported = utc_now().isoformat()
    session = a_share_session(now)
    quote_health = wallet.get("quote_health") if isinstance(wallet.get("quote_health"), dict) else {}
    quote_ok = quote_health.get("ok")
    fresh = True if session == "CLOSED" else quote_ok is not False
    detail_parts = [f"source={source_system}", f"mode={source_mode}", f"session={session}"]
    if session == "CLOSED":
        detail_parts.append("market_closed")
    if quote_ok is False and session == "OPEN":
        detail_parts.append("error=QUOTE_STALE")
        fresh = False

    positions = _map_positions(wallet.get("positions") or [], account_id)
    assert_no_symbol_collisions(positions)
    cash = decimal_string(wallet.get("cash_balance"), field="cash_balance")
    equity = decimal_string(wallet.get("total_asset"), field="total_asset")
    screen_results = _map_screen(screen)
    detail = "; ".join(detail_parts)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": str(uuid4()),
        "market": "A_SHARE",
        "account_id": account_id,
        "source_system": source_system,
        "source_mode": source_mode,
        "source_observed_at": observed,
        "exported_at": exported,
        "data_fresh": fresh,
        "degraded": not fresh,
        "detail": detail,
        "last_success_exported_at": exported if fresh else None,
        "last_error_type": None if fresh else "QUOTE_STALE",
        "last_error_at": None if fresh else exported,
        "health": {
            "system_ok": True,
            "data_fresh": fresh,
            "trading_channel_ok": False,
            "clock_skew_ms": 0,
            "degraded": not fresh,
            "detail": detail,
            "market_session": session,
        },
        "accounts": [
            {
                "account_id": account_id,
                "cash": cash,
                "equity": equity,
                "available_cash": cash,
                "frozen_cash": "0",
                "currency": "CNY",
                "reconciliation_version": "zisu-paper-v1",
                "source_observed_at": observed,
            }
        ],
        "positions": positions,
        "orders": [],
        "signals": [],
        "screen_results": screen_results,
        "fills": _map_fills(wallet.get("recent_trades") or [], account_id),
    }


def _map_positions(raw: Any, account_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("A_SHARE_WALLET_INCOMPLETE: positions must be a list")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("symbol") or "")
        symbol, source_symbol = normalize_ashare_symbol(source)
        qty = decimal_string(item.get("quantity"), field=f"{source}.quantity")
        available = item.get("sellable_quantity", item.get("quantity"))
        frozen = item.get("t_plus_one_locked", 0)
        rows.append(
            {
                "account_id": account_id,
                "symbol": symbol,
                "source_symbol": source_symbol,
                "quantity": qty,
                "available_quantity": decimal_string(available, field=f"{source}.sellable_quantity"),
                "frozen_quantity": decimal_string(frozen, field=f"{source}.t_plus_one_locked"),
                "avg_cost": decimal_string(item.get("avg_cost"), field=f"{source}.avg_cost"),
                "currency": "CNY",
            }
        )
    return rows


def _map_screen(screen: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = screen.get("actionable")
    if candidates is None:
        candidates = screen.get("candidates") or []
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        try:
            symbol, source_symbol = normalize_ashare_symbol(str(item["symbol"]))
        except ValueError:
            continue
        rows.append(
            {
                "kind": "SCREEN_RESULT",
                "symbol": symbol,
                "source_symbol": source_symbol,
                "policy_action": item.get("policy_action") or item.get("action"),
                "executable": item.get("executable"),
                "engine_id": item.get("engine_id"),
                "score": item.get("score"),
            }
        )
    return rows


def _map_fills(raw: Any, account_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("symbol") is None:
            continue
        try:
            symbol, source_symbol = normalize_ashare_symbol(str(item["symbol"]))
            side = str(item.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                continue
            rows.append(
                {
                    "account_id": account_id,
                    "symbol": symbol,
                    "source_symbol": source_symbol,
                    "side": "BUY" if side == "buy" else "SELL",
                    "quantity": decimal_string(item.get("quantity"), field="fill.quantity"),
                    "price": decimal_string(item.get("price"), field="fill.price"),
                    "source_trade_id": str(item.get("id") or ""),
                    "source_kind": "paper_trade",
                }
            )
        except ValueError:
            continue
    return rows
