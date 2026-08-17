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
        from dsh_contracts import Position
        return [Position(
            market=self.market, account_id="crypto-paper-1",
            symbol="BTC/USDT", quantity="0.01", available_quantity="0.01",
            avg_cost="65000", currency="USDT", as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        from dsh_contracts import AccountSummary
        return [AccountSummary(
            market=self.market, account_id="crypto-paper-1",
            cash="50000", equity="50650", currency="USDT",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

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
                account_id="paper-crypto-001",
                position_before="0", position_after="0.01",
                risk_budget_delta="6.5", worst_case_loss="6.5",
                as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        # 经 Gateway 的 Paper 提交是合法路径；红线是不绕过网关
        self.submitted = getattr(self, "submitted", [])
        self.submitted.append(intent)
        return f"{self.market.value}-ord-{len(self.submitted)}"

    def get_order_status(self, order_id):
        return {
            "order_id": order_id,
            "status": "FILLED",
            "symbol": "BTC/USDT",
            "filled_quantity": "0.01",
            "avg_price": "65000",
            "filled_at": datetime.now(UTC).isoformat(),
            "fees": "0",
        }

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
                        account_id="paper-crypto-001")
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


def _approve(approval_id: str):
    from dsh_contracts import ApprovalStatus
    resp = client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    assert resp.status_code == 200


def _make_risk_pass(monkeypatch):
    from quant_gateway.routers import orders as orders_router
    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {"passed": True, "limits_hit": []},
    )


def test_approved_signal_submits_paper_order(monkeypatch):
    _make_risk_pass(monkeypatch)
    agent, session = _agent_and_session()
    run_once(session, agent)  # tick 1：预览 + 发起审批

    approval_id = client.get("/v1/approvals?status=REQUESTED").json()[0]["approval_id"]
    _approve(approval_id)  # 人工批准

    run_once(session, agent)  # tick 2：恢复任务 → 提交 Paper 订单

    submitted = session.events.query("order/submitted")
    assert len(submitted) == 1
    assert submitted[0]["payload"]["approval_id"] == approval_id

    filled = session.events.query("order/filled")
    assert len(filled) == 1
    assert filled[0]["payload"]["order_id"] == submitted[0]["payload"]["order_id"]

    tasks = session.tasks.find_by_status("RECONCILED")
    assert len(tasks) == 1
    assert tasks[0]["order_id"].startswith("CRYPTO-ord-")

    # 审计日志：网关侧记录了订单提交
    audit = client.get("/v1/audit").json()
    assert any(e["action"] == "order.submitted" for e in audit)

    # tick 3：任务已完成，不重复提交
    run_once(session, agent)
    assert len(session.events.query("order/submitted")) == 1
    assert len(session.events.query("order/filled")) == 1


def test_rejected_signal_never_submits(monkeypatch):
    _make_risk_pass(monkeypatch)
    agent, session = _agent_and_session()
    run_once(session, agent)

    approval_id = client.get("/v1/approvals?status=REQUESTED").json()[0]["approval_id"]
    resp = client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "REJECTED", "decided_by": "alice"},
    )
    assert resp.status_code == 200

    run_once(session, agent)
    assert session.events.query("order/submitted") == []
    assert len(session.tasks.find_by_status("REJECTED")) == 1


def test_task_state_survives_runtime_restart(monkeypatch, tmp_path):
    """Session 重启（记忆/任务库重开）后任务状态不丢。"""
    monkeypatch.setenv("DSH_RUNTIME_DB", str(tmp_path / "runtime.db"))
    _make_risk_pass(monkeypatch)
    agent, session = _agent_and_session()
    run_once(session, agent)

    # 模拟重启：重置内存连接后重开同一 Session
    from dsh_runtime import reset, BotSession, load_profile
    reset()
    session2 = BotSession.for_profile(
        load_profile(PROFILES / "crypto-bot" / "profile.yaml")
    )
    pending = session2.tasks.find_by_status("AWAITING_APPROVAL")
    assert len(pending) == 1  # 任务还在等待人工，不会重复发起审批

    approval_id = pending[0]["approval_id"]
    _approve(approval_id)
    agent2, _ = _agent_and_session()
    run_once(session2, agent2)  # 重启后第一个 tick 完成提交
    assert len(session2.tasks.find_by_status("RECONCILED")) == 1
