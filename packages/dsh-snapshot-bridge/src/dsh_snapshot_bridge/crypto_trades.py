"""从 6celue trades.jsonl / equity_snapshots.jsonl 只读映射成交。

不从持仓或 pending_suggestions 推断成交。不读密钥。
"""

from __future__ import annotations

from typing import Any

from dsh_snapshot_bridge.decimalutil import decimal_string
from dsh_snapshot_bridge.symbols import normalize_crypto_symbol
from dsh_snapshot_bridge.timeutil import to_utc_iso


def map_crypto_closed_trades(
    rows: list[dict[str, Any]],
    *,
    account_id: str,
    limit: int = 80,
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):
        symbol_raw = str(row.get("symbol") or "")
        if not symbol_raw:
            continue
        if row.get("open_fill_price") in (None, "") or row.get("close_fill_price") in (None, ""):
            continue
        symbol, source_symbol = normalize_crypto_symbol(symbol_raw)
        trade_id = str(
            row.get("position_lifecycle_id")
            or row.get("order_id")
            or f"{symbol}:{row.get('opened_at')}:{row.get('closed_at')}"
        )
        if trade_id in seen:
            continue
        seen.add(trade_id)
        side_raw = str(row.get("side") or "").upper()
        side = "BUY" if side_raw in {"BUY", "LONG"} else "SELL" if side_raw in {"SELL", "SHORT"} else ""
        if not side:
            continue
        qty = row.get("close_fill_qty") or row.get("open_fill_qty") or row.get("quantity")
        mapped.append(
            {
                "account_id": account_id,
                "symbol": symbol,
                "source_symbol": source_symbol,
                "side": side,
                "quantity": decimal_string(qty, field="trade.quantity"),
                "entry_price": decimal_string(row.get("open_fill_price") or row.get("entry_price"), field="trade.entry"),
                "exit_price": decimal_string(row.get("close_fill_price") or row.get("exit_price"), field="trade.exit"),
                "fee": decimal_string(row.get("fee_usd") or 0, field="trade.fee"),
                "fee_currency": "USDT",
                "pnl": decimal_string(row.get("pnl_usd") or 0, field="trade.pnl"),
                "pnl_r": None if row.get("pnl_r") in (None, "") else str(row.get("pnl_r")),
                "exit_reason": str(row.get("exit_reason_cn") or row.get("exit_reason") or ""),
                "outcome": str(row.get("outcome") or ""),
                "opened_at": to_utc_iso(row.get("opened_at") or row.get("open_fill_ts")),
                "closed_at": to_utc_iso(row.get("closed_at") or row.get("close_fill_ts")),
                "source_trade_id": trade_id,
                "source_kind": "6celue_closed_trade",
                "signal_id": str(row.get("signal_id") or ""),
                "order_id": str(row.get("order_id") or row.get("open_client_order_id") or ""),
                "omega_at_entry": None if row.get("omega_at_entry") in (None, "") else str(row.get("omega_at_entry")),
                "signal_type": str(row.get("signal_type") or ""),
            }
        )
        if len(mapped) >= limit:
            break
    return mapped


def map_crypto_fills(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """一条闭环拆成开/平两腿，供执行质量统计。手续费记在平仓腿。"""
    fills: list[dict[str, Any]] = []
    for row in closed:
        common = {
            "account_id": row["account_id"],
            "symbol": row["symbol"],
            "source_symbol": row["source_symbol"],
            "quantity": row["quantity"],
            "source_trade_id": row["source_trade_id"],
        }
        fills.append(
            {
                **common,
                "side": row["side"],
                "price": row["entry_price"],
                "fee": "0",
                "source_kind": "6celue_open_fill",
                "filled_at": row.get("opened_at"),
            }
        )
        close_side = "SELL" if row["side"] == "BUY" else "BUY"
        fills.append(
            {
                **common,
                "side": close_side,
                "price": row["exit_price"],
                "fee": row["fee"],
                "pnl": row["pnl"],
                "exit_reason": row.get("exit_reason"),
                "source_kind": "6celue_close_fill",
                "filled_at": row.get("closed_at"),
            }
        )
    return fills


def map_crypto_equity_curve(rows: list[dict[str, Any]], *, limit: int = 96) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for row in rows[-limit:]:
        if row.get("equity") in (None, "") or row.get("ts") in (None, ""):
            continue
        mapped.append(
            {
                "ts": to_utc_iso(row.get("ts")),
                "equity": decimal_string(row.get("equity"), field="equity"),
                "wallet": decimal_string(row.get("wallet") or row.get("equity"), field="wallet"),
                "upnl": decimal_string(row.get("upnl") or 0, field="upnl"),
            }
        )
    return mapped
