from datetime import UTC, datetime

from fastapi import HTTPException

from dsh_contracts import HealthStatus, Market
from quant_gateway.adapters import ReadOnlyAdapter, register_adapter, wrap_readonly
from quant_gateway.adapters.base import MarketAdapter
from quant_gateway.adapters.registry import _adapters, get_adapter


class MiniAdapter(MarketAdapter):
    def __init__(self, market):
        self.market = market

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return []

    def get_account_summary(self):
        return []

    def get_signals(self):
        return []

    def preview_order(self, intent):
        return {}

    def request_order(self, intent):
        return "should-not-run"

    def get_order_status(self, order_id):
        return {"order_id": order_id, "status": "UNKNOWN"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id):
        pass

    def resume_strategy(self, strategy_id):
        pass

    def emergency_stop(self, account_id=None):
        pass


def test_readonly_refuses_writes():
    wrapper = ReadOnlyAdapter(MiniAdapter(Market.CRYPTO))
    try:
        wrapper.request_order({})
        raise AssertionError("expected 403")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_wrap_readonly_covers_registered_adapters():
    _adapters.clear()
    register_adapter(Market.CRYPTO, MiniAdapter(Market.CRYPTO))
    wrap_readonly()
    adapter = get_adapter(Market.CRYPTO)
    assert isinstance(adapter, ReadOnlyAdapter)
    try:
        adapter.request_order({})
        raise AssertionError("expected 403")
    except HTTPException as exc:
        assert exc.status_code == 403
    _adapters.clear()
