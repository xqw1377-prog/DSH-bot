"""文件快照适配器：只读行情/账户，不含交易密钥，也不下单。

Shadow 是 Bot 运行模式；本适配器是「真实行情/账户只读」的落地形态。
数据来自 QUANT_GATEWAY_SNAPSHOT_DIR 下的 {MARKET}.json，由外部系统导出。
写入一律失败关闭。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException
from dsh_contracts import (
    AccountSummary,
    HealthStatus,
    Market,
    OrderPreview,
    OrderSide,
    Position,
    RiskSnapshot,
    Signal,
)

from quant_gateway.adapters.base import MarketAdapter
from quant_gateway.adapters.registry import register_adapter


def _now() -> datetime:
    return datetime.now(UTC)


def _as_decimal(value, default="0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def fetch_public_price(symbol: str) -> Decimal | None:
    """公开行情只读叠加。未配置 URL 则跳过；失败返回 None，不编造价格。"""
    template = os.environ.get("QUANT_GATEWAY_PUBLIC_TICKER_URL", "").strip()
    if not template:
        return None
    url = template.replace("{symbol}", symbol)
    try:
        import httpx

        resp = httpx.get(url, timeout=2.0)
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        return None
    price = body.get("price") or body.get("last") or body.get("c")
    if price is None and isinstance(body, list) and body:
        price = body[0].get("price")
    if price is None:
        return None
    try:
        return Decimal(str(price))
    except Exception:
        return None


class SnapshotAdapter(MarketAdapter):
    def __init__(self, market: Market, path: Path) -> None:
        self.market = market
        self._path = path

    def _load(self) -> dict:
        if not self._path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"snapshot missing for {self.market.value}: {self._path}",
            )
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _parse_time(self, value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def get_health(self) -> HealthStatus:
        snap = self._load()
        raw = snap.get("health") or {}
        ticker_url = os.environ.get("QUANT_GATEWAY_PUBLIC_TICKER_URL", "").strip()
        data_fresh = bool(raw.get("data_fresh", snap.get("data_fresh", True)))
        detail = (
            raw.get("detail")
            or snap.get("detail")
            or f"snapshot {self._path.name}"
        )
        if ticker_url:
            symbols = [p.get("symbol") for p in (snap.get("positions") or []) if p.get("symbol")]
            prices = [fetch_public_price(str(s)) for s in symbols[:3]]
            if symbols and not any(prices):
                data_fresh = False
                detail = f"{detail}; public ticker unreachable"
            elif any(prices):
                detail = f"{detail}; public ticker overlaid"
        as_of = (
            self._parse_time(snap.get("exported_at"))
            or self._parse_time(raw.get("as_of"))
            or _now()
        )
        observed = self._parse_time(snap.get("source_observed_at"))
        return HealthStatus(
            market=self.market,
            system_ok=bool(raw.get("system_ok", True)),
            data_fresh=data_fresh,
            trading_channel_ok=bool(raw.get("trading_channel_ok", False)),
            clock_skew_ms=int(raw.get("clock_skew_ms", 0)),
            degraded=bool(raw.get("degraded", snap.get("degraded", False))) or not data_fresh,
            detail=detail,
            as_of=as_of,
            source_system=snap.get("source_system") or raw.get("source_system"),
            source_mode=snap.get("source_mode") or raw.get("source_mode"),
            source_observed_at=observed,
            snapshot_id=snap.get("snapshot_id"),
        )

    def get_positions(self, account_id: str | None = None) -> list[Position]:
        rows = []
        for item in self._load().get("positions") or []:
            if account_id and item.get("account_id") != account_id:
                continue
            symbol = str(item["symbol"])
            live = fetch_public_price(symbol)
            rows.append(
                Position(
                    market=self.market,
                    account_id=str(item["account_id"]),
                    symbol=symbol,
                    quantity=_as_decimal(item.get("quantity")),
                    available_quantity=_as_decimal(
                        item.get("available_quantity", item.get("quantity"))
                    ),
                    frozen_quantity=_as_decimal(item.get("frozen_quantity")),
                    avg_cost=live if live is not None else _as_decimal(item.get("avg_cost")),
                    currency=str(item.get("currency") or "USDT"),
                    as_of=_now(),
                )
            )
        return rows

    def get_account_summary(self) -> list[AccountSummary]:
        rows = []
        for item in self._load().get("accounts") or []:
            cash = _as_decimal(item.get("cash"))
            rows.append(
                AccountSummary(
                    market=self.market,
                    account_id=str(item["account_id"]),
                    cash=cash,
                    equity=_as_decimal(item.get("equity", cash)),
                    margin_used=(
                        _as_decimal(item["margin_used"])
                        if item.get("margin_used") is not None
                        else None
                    ),
                    available_cash=(
                        _as_decimal(item["available_cash"])
                        if item.get("available_cash") is not None
                        else cash
                    ),
                    frozen_cash=_as_decimal(item.get("frozen_cash")),
                    currency=str(item.get("currency") or "USDT"),
                    reconciliation_version=str(
                        item.get("reconciliation_version") or "snapshot-v1"
                    ),
                    as_of=_now(),
                )
            )
        return rows

    def get_signals(self) -> list[Signal]:
        rows = []
        for item in self._load().get("signals") or []:
            if str(item.get("kind") or "").upper() == "SCREEN_RESULT":
                continue
            generated = item.get("generated_at") or _now().isoformat()
            valid = item.get("valid_until") or _now().isoformat()
            rows.append(
                Signal(
                    signal_id=str(item["signal_id"]),
                    market=self.market,
                    strategy_id=str(item["strategy_id"]),
                    strategy_version=str(item.get("strategy_version") or "0.0.0"),
                    symbol=str(item["symbol"]),
                    side=OrderSide(item.get("side") or "BUY"),
                    strength=item.get("strength"),
                    generated_at=datetime.fromisoformat(str(generated).replace("Z", "+00:00")),
                    valid_until=datetime.fromisoformat(str(valid).replace("Z", "+00:00")),
                    data_snapshot_id=str(item.get("data_snapshot_id") or self._path.name),
                )
            )
        return rows

    def preview_order(self, intent) -> OrderPreview:
        from dsh_contracts import OrderIntent

        order_intent = (
            intent if isinstance(intent, OrderIntent) else OrderIntent.model_validate(intent)
        )
        price = Decimal("0")
        for pos in self.get_positions(order_intent.account_id):
            if pos.symbol == order_intent.symbol:
                price = pos.avg_cost
                break
        notional = order_intent.quantity * (price or Decimal("1"))
        return OrderPreview(
            intent=order_intent,
            estimated_cost=notional,
            estimated_slippage=Decimal("0"),
            risk=RiskSnapshot(
                risk_snapshot_id=order_intent.risk_snapshot_id,
                market=order_intent.market,
                account_id=order_intent.account_id,
                position_before=Decimal("0"),
                position_after=order_intent.quantity,
                risk_budget_delta=notional,
                worst_case_loss=notional,
                limits_hit=[],
                as_of=_now(),
            ),
        )

    def request_order(self, intent) -> str:
        raise HTTPException(
            status_code=403,
            detail="snapshot adapter is read-only; use Shadow bot mode, not request_order",
        )

    def get_order_status(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "UNKNOWN", "source": "snapshot"}

    def cancel_order(self, order_id: str) -> dict:
        raise HTTPException(status_code=403, detail="snapshot adapter refuses cancel")

    def pause_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="snapshot adapter refuses pause")

    def resume_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="snapshot adapter refuses resume")

    def emergency_stop(self, account_id: str | None = None) -> None:
        raise HTTPException(
            status_code=403, detail="snapshot adapter refuses emergency_stop"
        )


def register_snapshot_adapters(directory: str | None = None) -> None:
    root = Path(directory or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or "")
    if not root or not root.is_dir():
        return
    for market in Market:
        path = root / f"{market.value}.json"
        if path.is_file():
            register_adapter(market, SnapshotAdapter(market, path))
