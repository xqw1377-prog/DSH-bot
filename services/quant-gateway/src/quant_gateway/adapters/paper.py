"""本地联调用 Paper 适配器。

仅在环境变量 DSH_LOCAL_PAPER=1 时注册，不改变生产失败关闭行为。
账户 ID 来自统一配置（PAPER_*_ACCOUNT_ID），不在 runner / 适配器分别硬编码。
下单后立即纸面成交并落库，重启后可通过 get_order_status 恢复。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from dsh_contracts import (
    AccountSummary,
    HealthStatus,
    Market,
    OrderIntent,
    OrderPreview,
    OrderSide,
    Position,
    RiskSnapshot,
    Signal,
)

from quant_gateway import storage
from quant_gateway.adapters.base import MarketAdapter
from quant_gateway.adapters.registry import register_adapter
from quant_gateway.errors import structured_error


def _now() -> datetime:
    return datetime.now(UTC)


def paper_account_id(market: Market) -> str:
    if market == Market.A_SHARE:
        return os.environ.get("PAPER_A_SHARE_ACCOUNT_ID", "paper-a-share-001")
    return os.environ.get("PAPER_CRYPTO_ACCOUNT_ID", "paper-crypto-001")


class PaperAdapter(MarketAdapter):
    """代表现有量化系统的本地纸上实现。"""

    # 同步落库：按幂等键查不到 = 确定从未接受 venue。
    # 声明 STRONG 后，SUBMISSION_UNKNOWN 恢复路径才能释放键重试
    # （否则任务永远停在 UNKNOWN，只能人工修库）。
    order_lookup_consistency = "STRONG"

    def __init__(self, market: Market) -> None:
        self.market = market
        self._ids = count(1)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.paused: list[str] = []
        self.stopped = False
        self._account_id = paper_account_id(market)
        self._currency = "CNY" if market == Market.A_SHARE else "USDT"
        symbol, qty, price = (
            ("600519", Decimal("120"), Decimal("1680.50"))
            if market == Market.A_SHARE
            else ("BTCUSDT", Decimal("0.35"), Decimal("67420.00"))
        )
        self._symbol, self._qty, self._price = symbol, qty, price
        self._cash = Decimal("1048340") if market == Market.A_SHARE else Decimal("58403")
        self._orders: dict[str, dict] = {}

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market,
            system_ok=not self.stopped,
            data_fresh=True,
            trading_channel_ok=not self.stopped,
            clock_skew_ms=3,
            degraded=self.stopped,
            detail="local paper adapter" if not self.stopped else "emergency stop engaged",
            as_of=_now(),
        )

    def get_positions(self, account_id: str | None = None) -> list[Position]:
        if account_id and account_id != self._account_id:
            return []
        return [
            Position(
                market=self.market,
                account_id=self._account_id,
                symbol=self._symbol,
                quantity=self._qty,
                available_quantity=self._qty,
                frozen_quantity=Decimal("0"),
                avg_cost=self._price,
                currency=self._currency,
                as_of=_now(),
            )
        ]

    def get_account_summary(self) -> list[AccountSummary]:
        equity = self._cash + self._qty * self._price
        return [
            AccountSummary(
                market=self.market,
                account_id=self._account_id,
                cash=self._cash,
                equity=equity,
                margin_used=None if self.market == Market.A_SHARE else Decimal("4100"),
                available_cash=self._cash,
                frozen_cash=Decimal("0"),
                currency=self._currency,
                reconciliation_version="paper-v1",
                as_of=_now(),
            )
        ]

    def get_signals(self) -> list[Signal]:
        # 强度高于默认 min_strength(0.6)；valid_until 留足余量供审批后再校验
        now = _now()
        return [
            Signal(
                signal_id=f"{self.market.value}-sig-paper-1",
                market=self.market,
                strategy_id=(
                    "mean-reversion-ashare"
                    if self.market == Market.A_SHARE
                    else "funding-basis-crypto"
                ),
                strategy_version="0.1.0-paper",
                symbol=self._symbol,
                side=OrderSide.BUY,
                strength=0.75,
                generated_at=now,
                valid_until=now + timedelta(hours=24),
                data_snapshot_id="paper-data-1",
            )
        ]

    def preview_order(self, intent) -> OrderPreview:
        order_intent = (
            intent if isinstance(intent, OrderIntent) else OrderIntent.model_validate(intent)
        )
        notional = order_intent.quantity * self._price
        risk = RiskSnapshot(
            risk_snapshot_id=order_intent.risk_snapshot_id,
            market=order_intent.market,
            account_id=order_intent.account_id,
            position_before=self._qty,
            position_after=self._qty + (
                order_intent.quantity
                if order_intent.side == OrderSide.BUY
                else -order_intent.quantity
            ),
            risk_budget_delta=notional,
            worst_case_loss=notional * Decimal("0.01"),
            limits_hit=[],
            as_of=_now(),
        )
        return OrderPreview(
            intent=order_intent,
            estimated_cost=notional,
            estimated_slippage=Decimal("0.0005"),
            risk=risk,
        )

    def request_order(self, intent) -> str:
        if self.stopped:
            raise structured_error(
                409,
                error_code="TRADING_HALTED",
                phase="PRE_SUBMIT",
                retryable=True,
                submission_unknown=False,
                message="emergency stop engaged; order rejected",
            )
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        if payload.get("account_id") != self._account_id:
            raise ValueError(
                f"unknown paper account_id={payload.get('account_id')}; "
                f"expected={self._account_id}"
            )
        order_id = f"{self.market.value}-paper-{next(self._ids)}"
        avg_price = self._price
        filled_at = _now().isoformat()
        quantity = Decimal(str(payload.get("quantity", "0")))
        fees = Decimal("0")
        forced = os.environ.get("PAPER_ORDER_OUTCOME", "FILLED").upper()
        position_before = self._qty
        cash_before = self._cash
        status = forced if forced in {
            "FILLED", "PARTIALLY_FILLED", "REJECTED", "UNKNOWN", "CANCELLED",
        } else "FILLED"
        if status == "FILLED":
            if payload.get("side") == "BUY":
                self._qty += quantity
                self._cash -= quantity * avg_price + fees
            else:
                self._qty -= quantity
                self._cash += quantity * avg_price - fees
        filled_qty = quantity if status == "FILLED" else (
            quantity / 2 if status == "PARTIALLY_FILLED" else Decimal("0")
        )
        record = {
            "order_id": order_id,
            "status": status,
            "market": self.market.value,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "filled_quantity": str(filled_qty),
            "avg_price": str(avg_price),
            "filled_at": filled_at,
            "fees": str(fees),
            "taxes": "0",
            "fills": [
                {
                    "quantity": str(filled_qty),
                    "price": str(avg_price),
                    "fee": str(fees),
                }
            ],
            "position_before": str(position_before),
            "cash_before": str(cash_before),
            "position_after": str(self._qty),
            "cash_after": str(self._cash),
            "source": "paper",
            "account_id": self._account_id,
            "intent": payload,
        }
        self.submitted.append(payload)
        self._orders[order_id] = record
        storage.save_paper_order(order_id, self.market.value, record)
        return order_id

    def find_order_by_idempotency_key(self, key: str) -> dict | None:
        # Paper 同步落库：按幂等键能查到即已接单；查不到即确定从未接受
        found = storage.find_paper_order_by_idempotency_key(key)
        if found is not None:
            return dict(found)
        return None

    def get_order_status(self, order_id: str) -> dict:
        if order_id in self._orders:
            return dict(self._orders[order_id])
        stored = storage.get_paper_order(order_id)
        if stored is not None:
            self._orders[order_id] = stored
            return dict(stored)
        return {"order_id": order_id, "status": "UNKNOWN", "source": "paper"}

    def cancel_order(self, order_id: str) -> dict:
        self.cancelled.append(order_id)
        return {"order_id": order_id, "status": "CANCELLED", "source": "paper"}

    def pause_strategy(self, strategy_id: str) -> None:
        if strategy_id not in self.paused:
            self.paused.append(strategy_id)

    def resume_strategy(self, strategy_id: str) -> None:
        if strategy_id in self.paused:
            self.paused.remove(strategy_id)

    def emergency_stop(self, account_id: str | None = None) -> None:
        self.stopped = True

    def resume_trading(self) -> None:
        self.stopped = False


def register_paper_adapters() -> None:
    register_adapter(Market.A_SHARE, PaperAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, PaperAdapter(Market.CRYPTO))
