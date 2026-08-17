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
from quant_gateway.routers import orders


class FakeAdapter(MarketAdapter):
    """代表现有量化系统的内存实现，仅用于测试。"""

    def __init__(self, market: Market) -> None:
        self.market = market
        self._ids = count(1)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.paused: list[str] = []
        self.stopped = False

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market,
            system_ok=True,
            data_fresh=True,
            trading_channel_ok=True,
            clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id: str | None = None) -> list:
        return []

    def get_account_summary(self) -> list:
        return []

    def get_signals(self) -> list:
        return []

    def preview_order(self, intent):
        return {"intent": intent, "estimated_cost": "0", "estimated_slippage": "0"}

    def request_order(self, intent) -> str:
        order_id = f"{self.market}-ord-{next(self._ids)}"
        self.submitted.append(intent)
        return order_id

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


@pytest.fixture(autouse=True)
def reset_gateway_state():
    """每个测试前重置适配器注册表与内存账本。"""
    from quant_gateway import approval_store

    _adapters.clear()
    approval_store.reset()
    register_adapter(Market.A_SHARE, FakeAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, FakeAdapter(Market.CRYPTO))
    yield
    _adapters.clear()
