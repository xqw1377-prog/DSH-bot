"""A 股 Bot 端到端集成测试。

覆盖：正常 tick、记忆去重、弱信号跳过、T+1 卖出约束（无可用持仓则跳过 SELL 信号）。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, OrderSide,
    Position, RiskSnapshot, Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class AStockFakeAdapter(MarketAdapter):
    """可配置信号的假 A 股量化系统。"""
    def __init__(self, market: Market, signals=None, positions=None):
        self.market = market
        self._signals = signals or []
        self._positions = positions or []

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return self._positions

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="a-stock-paper-1",
            cash="100000", equity="100000", currency="CNY",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        return self._signals

    def preview_order(self, intent):
        return OrderPreview(
            intent=intent, estimated_cost="1000", estimated_slippage="0.1",
            risk=RiskSnapshot(
                risk_snapshot_id="rs-preview", market=self.market,
                account_id="a-stock-paper-1",
                position_before="0", position_after="100",
                risk_budget_delta="1000", worst_case_loss="100",
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


def _signal(side=OrderSide.BUY, strength=0.8, symbol="600519.SH"):
    now = datetime.now(UTC)
    return Signal(
        signal_id="sig-001", market=Market.A_SHARE,
        strategy_id="mean-reversion", strategy_version="1.0.0",
        symbol=symbol, side=side, strength=strength,
        generated_at=now, valid_until=now + timedelta(minutes=30),
        data_snapshot_id="snap-1",
    )


def _position(symbol="600519.SH", available="0"):
    return Position(
        market=Market.A_SHARE, account_id="a-stock-paper-1",
        symbol=symbol, quantity=Decimal("100"),
        available_quantity=Decimal(available),
        frozen_quantity=Decimal("0"), avg_cost=Decimal("10"),
        currency="CNY", as_of=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def setup_gateway():
    from quant_gateway import approval_store

    approval_store.reset()
    register_adapter(Market.A_SHARE, AStockFakeAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, AStockFakeAdapter(Market.CRYPTO))
    reset()
    yield
    approval_store.reset()
    reset()


def _agent_and_session(signals=None, positions=None):
    register_adapter(Market.A_SHARE,
                     AStockFakeAdapter(Market.A_SHARE, signals, positions))
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client

    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client

    from dsh_a_stock_agent import AStockAgent
    agent = AStockAgent(gateway=gateway, approvals=approvals,
                        account_id="a-stock-paper-1")
    profile = load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    return agent, BotSession.for_profile(profile)


def test_buy_signal_creates_approval():
    agent, session = _agent_and_session(signals=[_signal(OrderSide.BUY)])
    run_once(session, agent)

    approvals = client.get("/v1/approvals?status=REQUESTED").json()
    assert len(approvals) == 1
    assert approvals[0]["subject_id"] == "sig-001"
    events = session.events.query("approval/requested")
    assert events[0]["actor"]["id"] == "a-stock-bot"


def test_second_tick_does_not_duplicate():
    agent, session = _agent_and_session(signals=[_signal()])
    run_once(session, agent)
    run_once(session, agent)
    assert len(client.get("/v1/approvals").json()) == 1


def test_weak_signal_skipped():
    agent, session = _agent_and_session(signals=[_signal(strength=0.2)])
    run_once(session, agent)
    assert client.get("/v1/approvals").json() == []
    assert session.memory.has_tagged("signal:sig-001")


def test_sell_without_available_position_skipped_due_to_t_plus_1():
    """T+1：无可用持仓的 SELL 信号应跳过。"""
    agent, session = _agent_and_session(
        signals=[_signal(OrderSide.SELL)],
        positions=[],  # 无持仓
    )
    run_once(session, agent)
    assert client.get("/v1/approvals").json() == []
    memos = session.memory.recent(kind="signal-skip")
    assert any("T+1" in m["content"] for m in memos)


def test_sell_with_available_position_proceeds():
    """T+1：有可用持仓的 SELL 信号应正常发起审批。"""
    agent, session = _agent_and_session(
        signals=[_signal(OrderSide.SELL)],
        positions=[_position(available="100")],
    )
    run_once(session, agent)
    assert len(client.get("/v1/approvals").json()) == 1
