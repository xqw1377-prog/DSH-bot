"""Gateway 测试脚手架。

网关默认对未注册适配器的市场失败关闭（503），因此订单路径的测试
必须显式注册一个内存假适配器，代表「现有量化系统」。

模块级的幂等键、风险快照和审批记录在测试间自动清理，
避免测试顺序影响结果——尤其是失败关闭用例依赖「快照不存在」。
"""

from datetime import UTC, datetime
from itertools import count

import pytest

from dsh_contracts import HealthStatus, Market
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.adapters.registry import _adapters


class FakeAdapter(MarketAdapter):
    """代表现有量化系统的内存实现，仅用于测试。"""

    def __init__(self, market: Market) -> None:
        self.market = market
        self._ids = count(1)
        self._orders_by_key: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.paused: list[str] = []
        self.stopped = False

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market,
            system_ok=not self.stopped,
            data_fresh=True,
            trading_channel_ok=not self.stopped,
            clock_skew_ms=0,
            degraded=self.stopped,
            detail="emergency stop engaged" if self.stopped else None,
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id: str | None = None) -> list:
        return []

    def get_account_summary(self) -> list:
        from decimal import Decimal

        from dsh_contracts import AccountSummary

        account = "acc-1" if self.market == Market.A_SHARE else "crypto-paper-1"
        return [AccountSummary(
            market=self.market,
            account_id=account,
            cash=Decimal("1000000"),
            equity=Decimal("1200000"),
            currency="CNY" if self.market == Market.A_SHARE else "USDT",
            reconciliation_version="v1",
            as_of=datetime.now(UTC),
        )]

    def get_signals(self) -> list:
        return []

    def preview_order(self, intent):
        return {"intent": intent, "estimated_cost": "0", "estimated_slippage": "0"}

    def request_order(self, intent) -> str:
        payload = intent if isinstance(intent, dict) else intent
        order_id = f"{self.market}-ord-{next(self._ids)}"
        self.submitted.append(payload)
        key = payload.get("idempotency_key") if isinstance(payload, dict) else payload.idempotency_key
        self._orders_by_key[key] = {"order_id": order_id}
        return order_id

    def find_order_by_idempotency_key(self, key: str) -> dict | None:
        # 内存账本同步写入：查不到即确定从未接受
        return self._orders_by_key.get(key)

    def get_order_status(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "ACKNOWLEDGED"}

    def cancel_order(self, order_id: str) -> dict:
        self.cancelled.append(order_id)
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id: str) -> None:
        self.paused.append(strategy_id)

    def resume_strategy(self, strategy_id: str) -> None:
        if strategy_id in self.paused:
            self.paused.remove(strategy_id)

    def emergency_stop(self, account_id: str | None = None) -> None:
        self.stopped = True

    def resume_trading(self) -> None:
        self.stopped = False


def install_fake_adapters() -> None:
    _adapters.clear()
    register_adapter(Market.A_SHARE, FakeAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, FakeAdapter(Market.CRYPTO))


@pytest.fixture(autouse=True)
def reset_gateway_state():
    """每个测试前重置适配器注册表与内存账本。"""
    from quant_gateway import approval_store

    approval_store.reset()
    install_fake_adapters()
    yield
    _adapters.clear()
