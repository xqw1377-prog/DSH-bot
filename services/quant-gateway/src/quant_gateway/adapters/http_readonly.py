"""现有币量化系统的只读 HTTP 适配器。

配置 QUANT_CRYPTO_READONLY_URL 后注册到 CRYPTO。
只读：health / positions / accounts / signals / order status。
任何资金动作失败关闭。这不是实盘下单通道。
"""

from __future__ import annotations

import os
from datetime import timedelta, UTC, datetime
from decimal import Decimal

import httpx
from fastapi import HTTPException

from dsh_contracts import (
    AccountSummary,
    HealthStatus,
    Market,
    Position,
    Signal,
)
from quant_gateway.adapters.base import MarketAdapter
from quant_gateway.adapters.registry import register_adapter


class HttpReadOnlyAdapter(MarketAdapter):
    order_lookup_consistency = "EVENTUAL"

    def __init__(self, base_url: str, market: Market = Market.CRYPTO) -> None:
        self._base = base_url.rstrip("/")
        self.market = market

    def _get(self, path: str) -> dict | list:
        try:
            resp = httpx.get(f"{self._base}{path}", timeout=3.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"readonly upstream unavailable: {exc}",
            ) from exc

    def get_health(self):
        raw = self._get("/healthz")
        if not isinstance(raw, dict):
            raw = {}
        return HealthStatus(
            market=self.market,
            system_ok=bool(raw.get("system_ok", raw.get("status") == "ok")),
            data_fresh=bool(raw.get("data_fresh", True)),
            trading_channel_ok=bool(raw.get("trading_channel_ok", False)),
            clock_skew_ms=int(raw.get("clock_skew_ms") or 0),
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id: str | None = None):
        raw = self._get("/positions")
        items = raw if isinstance(raw, list) else raw.get("positions", [])
        out = []
        for item in items:
            if account_id and item.get("account_id") != account_id:
                continue
            out.append(Position(
                market=self.market,
                account_id=item.get("account_id", ""),
                symbol=item.get("symbol", ""),
                quantity=Decimal(str(item.get("quantity", "0"))),
                available_quantity=Decimal(str(item.get("available_quantity", item.get("quantity", "0")))),
                frozen_quantity=Decimal(str(item.get("frozen_quantity", "0"))),
                avg_cost=Decimal(str(item.get("avg_cost", "0"))),
                currency=item.get("currency", "USDT"),
                as_of=datetime.now(UTC),
            ))
        return out

    def get_account_summary(self):
        raw = self._get("/accounts")
        items = raw if isinstance(raw, list) else raw.get("accounts", [])
        return [
            AccountSummary(
                market=self.market,
                account_id=item.get("account_id", ""),
                cash=Decimal(str(item.get("cash", "0"))),
                equity=Decimal(str(item.get("equity", "0"))),
                currency=item.get("currency", "USDT"),
                reconciliation_version=item.get("reconciliation_version", "v1"),
                as_of=datetime.now(UTC),
            )
            for item in items
        ]

    def get_signals(self):
        raw = self._get("/signals")
        items = raw if isinstance(raw, list) else raw.get("signals", [])
        # 时间戳以上游为准;此前一律置 now,快照信号下一秒就被判过期
        now = datetime.now(UTC)
        horizon = timedelta(minutes=30)

        def _ts(item, key):
            value = item.get(key)
            if value:
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                except ValueError:
                    pass
            return now
        return [
            Signal(
                signal_id=item.get("signal_id", ""),
                market=self.market,
                strategy_id=item.get("strategy_id", "upstream"),
                strategy_version=item.get("strategy_version", "0"),
                symbol=item.get("symbol", ""),
                side=item.get("side", "BUY"),
                strength=item.get("strength"),
                generated_at=_ts(item, 'generated_at'),
                valid_until=_ts(item, 'valid_until') if item.get('valid_until') else (now + horizon),
                data_snapshot_id=item.get("data_snapshot_id", "upstream"),
            )
            for item in items
        ]

    def preview_order(self, intent):
        raise HTTPException(status_code=403, detail="readonly adapter refuses preview")

    def request_order(self, intent) -> str:
        raise HTTPException(status_code=403, detail="readonly adapter refuses request_order")

    def get_order_status(self, order_id: str) -> dict:
        raw = self._get(f"/orders/{order_id}")
        if not isinstance(raw, dict):
            return {"order_id": order_id, "status": "UNKNOWN", "source": "readonly"}
        raw.setdefault("order_id", order_id)
        raw.setdefault("source", "readonly")
        return raw

    def cancel_order(self, order_id: str) -> dict:
        raise HTTPException(status_code=403, detail="readonly adapter refuses cancel")

    def pause_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="readonly adapter refuses pause")

    def resume_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="readonly adapter refuses resume")

    def emergency_stop(self, account_id: str | None = None) -> None:
        raise HTTPException(status_code=403, detail="readonly adapter refuses emergency_stop")


def register_http_readonly_adapters() -> None:
    url = os.environ.get("QUANT_CRYPTO_READONLY_URL", "").strip()
    if not url:
        return
    register_adapter(Market.CRYPTO, HttpReadOnlyAdapter(url, Market.CRYPTO))
