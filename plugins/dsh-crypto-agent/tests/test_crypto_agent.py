"""Crypto Bot 端到端集成测试。

真实链路：DSH Session 加载 Profile → 插件 Agent tick →
Quant Gateway（FastAPI 应用）读取信号 → 订单预览 → 发起审批 →
事件与记忆持久化。全程无真实交易所。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_contracts import (
    HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, Profile, load_profile, run_once
from dsh_trade_approval import ApprovalWorkflow
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class SignalFakeAdapter(MarketAdapter):
    """带一条真实形状信号的假量化系统。"""

    def __init__(self, market: Market):
        self.market = market

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return []

    def get_account_summary(self):
        return []

    def get_signals(self):
        now = datetime.now(UTC)
        return [Signal(
            signal_id="sig-001", market=self.market,
            strategy_id="trend-momentum", strategy_version="1.2.0",
            symbol="BTC/USDT", side="BUY", strength=0.8,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-1",
        )]

    def preview_order(self, intent):
        return OrderPreview(
            intent=intent, estimated_cost="650", estimated_slippage="0.5",
            risk=RiskSnapshot(
                risk_snapshot_id="rs-preview", market=self.market,
                account_id="crypto-paper-1",
                position_before="0", position_after="0.01",
                risk_budget_delta="6.5", worst_case_loss="6.5",
                as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        raise AssertionError("Agent 不允许直接下单")

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
    register_adapter(Market.CRYPTO, SignalFakeAdapter(Market.CRYPTO))
    register_adapter(Market.A_SHARE, SignalFakeAdapter(Market.A_SHARE))
    from dsh_runtime import reset
    reset()
    yield
    approval_store.reset()
    reset()


def _agent_and_session():
    # TestClient 兼容 httpx.Client 的 get/post 接口子集，直连网关应用不占端口
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client

    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client

    from dsh_crypto_agent import CryptoAgent
    agent = CryptoAgent(gateway=gateway, approvals=approvals,
                        account_id="crypto-paper-1")
    profile = load_profile(PROFILES / "crypto-bot" / "profile.yaml")
    return agent, BotSession.for_profile(profile)


def test_full_tick_creates_approval_event_and_memory():
    agent, session = _agent_and_session()
    run_once(session, agent)

    # 审批已在 Gateway 创建，等待人工
    approvals = client.get("/v1/approvals?status=REQUESTED").json()
    assert len(approvals) == 1
    assert approvals[0]["subject_id"] == "sig-001"
    assert approvals[0]["evidence_refs"][0] == "signal:sig-001"

    # 事件留痕
    events = session.events.query("approval/requested")
    assert len(events) == 1
    assert events[0]["actor"]["id"] == "crypto-bot"

    # 记忆已记录该信号
    assert session.memory.has_tagged("signal:sig-001")


def test_second_tick_does_not_duplicate_approval():
    agent, session = _agent_and_session()
    run_once(session, agent)
    run_once(session, agent)
    approvals = client.get("/v1/approvals").json()
    assert len(approvals) == 1  # 记忆去重生效


def test_weak_signal_skips_approval():
    adapter = SignalFakeAdapter(Market.CRYPTO)
    original = adapter.get_signals

    def weak_signals():
        signals = original()
        return [signals[0].model_copy(update={"strength": 0.2})]

    adapter.get_signals = weak_signals  # type: ignore[assignment]
    register_adapter(Market.CRYPTO, adapter)

    agent, session = _agent_and_session()
    run_once(session, agent)
    assert client.get("/v1/approvals").json() == []
    assert session.memory.has_tagged("signal:sig-001")  # 记录为已忽略
