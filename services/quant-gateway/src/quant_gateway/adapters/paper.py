"""本地联调用 Paper 适配器。

仅在环境变量 DSH_LOCAL_PAPER=1 时注册，不改变生产失败关闭行为。
数据全部内存构造，不连接真实券商或交易所。
"""

from datetime import UTC, datetime
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

from quant_gateway.adapters.base import MarketAdapter
from quant_gateway.adapters.registry import register_adapter


def _now() -> datetime:
    return datetime.now(UTC)


class PaperAdapter(MarketAdapter):
    """代表现有量化系统的本地纸上实现。"""

    def __init__(self, market: Market) -> None:
        self.market = market
        self._ids = count(1)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.paused: list[str] = []
        self.stopped = False
        self._account_id = (
            "paper-a-share-001" if market == Market.A_SHARE else "paper-crypto-001"
        )
        self._currency = "CNY" if market == Market.A_SHARE else "USDT"
        self._symbols = (
            (("600519", Decimal("120"), Decimal("1680.50")),)
            if market == Market.A_SHARE
            else (("BTCUSDT", Decimal("0.35"), Decimal("67420.00")),)
        )

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
                symbol=symbol,
                quantity=qty,
                available_quantity=qty,
                frozen_quantity=Decimal("0"),
                avg_cost=cost,
                currency=self._currency,
                as_of=_now(),
            )
            for symbol, qty, cost in self._symbols
        ]

    def get_account_summary(self) -> list[AccountSummary]:
        equity = Decimal("1250000") if self.market == Market.A_SHARE else Decimal("82000")
        cash = Decimal("1048340") if self.market == Market.A_SHARE else Decimal("58403")
        return [
            AccountSummary(
                market=self.market,
                account_id=self._account_id,
                cash=cash,
                equity=equity,
                margin_used=None if self.market == Market.A_SHARE else Decimal("4100"),
                currency=self._currency,
                reconciliation_version="paper-v1",
                as_of=_now(),
            )
        ]

    def get_signals(self) -> list[Signal]:
        symbol = self._symbols[0][0]
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
                symbol=symbol,
                side=OrderSide.BUY,
                strength=0.42,
                generated_at=_now(),
                valid_until=_now(),
                data_snapshot_id="paper-data-1",
            )
        ]

    def preview_order(self, intent) -> OrderPreview:
        order_intent = (
            intent if isinstance(intent, OrderIntent) else OrderIntent.model_validate(intent)
        )
        notional = order_intent.quantity * Decimal("100")
        risk = RiskSnapshot(
            risk_snapshot_id=order_intent.risk_snapshot_id,
            market=order_intent.market,
            account_id=order_intent.account_id,
            position_before=Decimal("0"),
            position_after=order_intent.quantity,
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
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        order_id = f"{self.market.value}-paper-{next(self._ids)}"
        self.submitted.append(payload)
        return order_id

    def get_order_status(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "ACKNOWLEDGED", "source": "paper"}

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


def register_paper_adapters() -> None:
    register_adapter(Market.A_SHARE, PaperAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, PaperAdapter(Market.CRYPTO))
