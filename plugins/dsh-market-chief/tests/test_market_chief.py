"""Market Chief 总控插件集成测试。

真实链路：DSH Session → MarketChiefAgent tick → Quant Gateway（TestClient）→
跨市场健康检查、状态卡片、待审批待办、降级 incident 事件。全程无真实交易所。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, Profile, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class _State:
    """跨测试共享的可调假适配器。"""
    def __init__(self):
        self.health = {
            Market.A_SHARE: self._ok(Market.A_SHARE),
            Market.CRYPTO: self._ok(Market.CRYPTO),
        }
        self.accounts = {
            Market.A_SHARE: [self._acct(Market.A_SHARE)],
            Market.CRYPTO: [self._acct(Market.CRYPTO)],
        }

    @staticmethod
    def _ok(m: Market) -> HealthStatus:
        return HealthStatus(
            market=m, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    @staticmethod
    def _acct(m: Market) -> AccountSummary:
        return AccountSummary(
            market=m, account_id=f"{m.value.lower()}-paper-1",
            cash="10000", equity="10000", currency="CNY",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )


class ChiefFakeAdapter(MarketAdapter):
    """共享状态假适配器：Market Chief 调度读，专业 Bot 写。"""
    state: _State = _State()

    def __init__(self, market: Market):
        self.market = market

    def get_health(self) -> HealthStatus:
        return self.state.health[self.market]

    def get_positions(self, account_id=None):
        return []

    def get_account_summary(self):
        return self.state.accounts[self.market]

    def get_signals(self):
        return [Signal(
            signal_id="sig-001", market=self.market,
            strategy_id="trend-momentum", strategy_version="1.2.0",
            symbol="BTC/USDT" if self.market == Market.CRYPTO else "600519.SH",
            side="BUY", strength=0.8,
            generated_at=datetime.now(UTC),
            valid_until=datetime.now(UTC) + timedelta(minutes=30),
            data_snapshot_id="snap-1",
        )]

    def preview_order(self, intent):
        return OrderPreview(
            intent=intent, estimated_cost="650", estimated_slippage="0.5",
            risk=RiskSnapshot(
                risk_snapshot_id="rs-preview", market=self.market,
                account_id="paper-1",
                position_before="0", position_after="0.01",
                risk_budget_delta="6.5", worst_case_loss="6.5",
                as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        raise AssertionError("Market Chief 不允许直接下单")

    def get_order_status(self, order_id):
        return {"order_id": order_id, "status": "FILLED"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id):
        pass

    def resume_strategy(self, strategy_id):
        pass

    def emergency_stop(self, account_id=None):
        pass


@pytest.fixture(autouse=True)
def setup_gateway():
    from quant_gateway import approval_store

    approval_store.reset()
    ChiefFakeAdapter.state = _State()
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO))
    reset()
    yield
    approval_store.reset()
    reset()


def _agent_and_session():
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client

    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client

    from dsh_market_chief import MarketChiefAgent
    agent = MarketChiefAgent(gateway=gateway, approvals=approvals)
    profile = load_profile(PROFILES / "market-chief" / "profile.yaml")
    return agent, BotSession.for_profile(profile)


def test_tick_produces_cross_market_summary():
    agent, session = _agent_and_session()
    run_once(session, agent)

    memos = session.memory.recent(kind="market-summary")
    assert len(memos) == 1
    summary = memos[0]["content"]
    assert "A_SHARE" in summary
    assert "CRYPTO" in summary
    assert "正常" in summary  # 两市场都健康


def test_degraded_market_emits_incident():
    # 把 CRYPTO 标记为降级
    ChiefFakeAdapter.state.health[Market.CRYPTO] = HealthStatus(
        market=Market.CRYPTO, system_ok=False, data_fresh=True,
        trading_channel_ok=True, clock_skew_ms=0,
        as_of=datetime.now(UTC),
    )
    agent, session = _agent_and_session()
    run_once(session, agent)

    incidents = session.events.query("incident/opened")
    assert len(incidents) == 1
    assert incidents[0]["actor"]["id"] == "market-chief"
    assert "CRYPTO" in incidents[0]["market"]

    advice = session.memory.recent(kind="advice")
    assert any("降级" in m["content"] for m in advice)


def test_pending_approvals_become_todo():
    # 通过审批接口预先注入一条待审批
    client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "order",
        "subject_id": "sig-001",
        "evidence_refs": ["signal:sig-001"],
    })

    agent, session = _agent_and_session()
    run_once(session, agent)

    todos = session.memory.recent(kind="todo")
    assert any("1 项待审批" in t["content"] for t in todos)


def test_health_check_failure_is_fail_closed():
    # 用一个会抛错的适配器替换
    class BadAdapter(ChiefFakeAdapter):
        def get_health(self):
            raise RuntimeError("upstream down")

    register_adapter(Market.CRYPTO, BadAdapter(Market.CRYPTO))
    agent, session = _agent_and_session()
    run_once(session, agent)  # 不应外抛

    incidents = session.events.query("incident/opened")
    assert any("CRYPTO" in i["market"] for i in incidents)
    # A 股仍应正常汇总
    summaries = session.memory.recent(kind="market-summary")
    assert "A_SHARE" in summaries[0]["content"]
