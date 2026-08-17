"""Market Chief 汇总测试：只读查询、降级告警、审批计数。"""

from datetime import UTC, datetime

import pytest
from dsh_contracts import HealthStatus, Market
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, Profile, load_profile, run_once, reset
from pathlib import Path
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway import approval_store

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


class ChiefFakeAdapter(MarketAdapter):
    def __init__(self, market, healthy=True):
        self.market, self.healthy = market, healthy

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=self.healthy, data_fresh=self.healthy,
            trading_channel_ok=True, clock_skew_ms=0,
            degraded=not self.healthy, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None): return []
    def get_account_summary(self): return []
    def get_signals(self): return []
    def preview_order(self, intent): return {}
    def request_order(self, intent):
        raise AssertionError("Chief 不允许下单")
    def get_order_status(self, order_id): return {}
    def cancel_order(self, order_id): return {}
    def pause_strategy(self, sid): pass
    def resume_strategy(self, sid): pass
    def emergency_stop(self, account_id=None): pass


@pytest.fixture(autouse=True)
def setup():
    approval_store.reset()
    reset()
    yield
    approval_store.reset()
    reset()


def _chief_and_session():
    from fastapi.testclient import TestClient
    from quant_gateway.main import app
    from dsh_market_chief import MarketChief

    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = TestClient(app)
    profile = load_profile(PROFILES / "market-chief" / "profile.yaml")
    return MarketChief(gateway=gateway), BotSession.for_profile(profile)


def test_chief_summarizes_health_and_pending_approvals():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=True))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))
    approval_store.create_approval(
        market=Market.CRYPTO, requested_by_bot="crypto-bot",
        subject_type="order", subject_id="s1",
    )

    chief, session = _chief_and_session()
    run_once(session, chief)

    summaries = session.events.query("market/chief.summary")
    assert len(summaries) == 1
    assert summaries[0]["payload"]["pending_approvals"] == 1
    assert summaries[0]["payload"]["degraded"] == []
    assert session.events.query("incident/opened") == []


def test_chief_alerts_on_degraded_market():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=False))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))

    chief, session = _chief_and_session()
    run_once(session, chief)

    incidents = session.events.query("incident/opened")
    assert len(incidents) == 1
    assert "A_SHARE" in incidents[0]["payload"]["markets"]
