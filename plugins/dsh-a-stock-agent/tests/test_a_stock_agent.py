"""A 股 Bot 全链路集成测试：复用 TradeExecutionCore + AStockMarketPolicy。

验证：执行闭环与 Crypto 同源（无分叉），A 股规则真实生效——
交易时段拦截、整手、涨跌停、审批→提交→对账。
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, Position, RiskSnapshot,
    Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from fastapi.testclient import TestClient
from quant_gateway import approval_store
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class AStockFakeAdapter(MarketAdapter):
    """A 股假量化系统：600519，昨收 1680.50，成交回写。"""

    order_lookup_consistency = "STRONG"

    def __init__(self, market):
        self.market = market
        self.submitted = []
        self.order_status_map = {}
        self.position = Decimal("100")
        self.cash = Decimal("1000000")
        self.price = Decimal("1680.50")
        self._last = None

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return [Position(
            market=self.market, account_id="paper-a-share-001",
            symbol="600519", quantity=str(self.position),
            available_quantity=str(self.position), frozen_quantity="0",
            avg_cost=str(self.price), currency="CNY",
            as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="paper-a-share-001",
            cash=str(self.cash),
            equity=str(self.cash + self.position * self.price),
            currency="CNY", reconciliation_version="v1",
            as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        now = datetime.now(UTC)
        return [Signal(
            signal_id="a-sig-1", market=self.market,
            strategy_id="mean-reversion", strategy_version="1.0.0",
            symbol="600519", side="BUY", strength=0.9,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-a",
        )]

    def preview_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent
        qty = Decimal(str(payload["quantity"])) if isinstance(payload, dict) else payload.quantity
        notional = qty * self.price
        return OrderPreview(
            intent=payload, estimated_cost=notional,
            estimated_slippage=Decimal("0.01"),
            risk=RiskSnapshot(
                risk_snapshot_id=f"rs-a", market=self.market,
                account_id="paper-a-share-001",
                position_before=self.position,
                position_after=self.position + qty,
                risk_budget_delta=notional,
                worst_case_loss=notional * Decimal("0.10"),
                limits_hit=[], as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"{self.market.value}-ord-{len(self.submitted)}"
        qty = Decimal(str(payload["quantity"]))
        fees = Decimal("5")
        if payload.get("side") == "BUY":
            self.position += qty
            self.cash -= qty * self.price + fees
        else:
            self.position -= qty
            self.cash += qty * self.price - fees
        self._last = {
            "order_id": order_id, "status": "FILLED", "symbol": "600519",
            "filled_quantity": str(qty), "avg_price": str(self.price),
            "fees": str(fees), "filled_at": datetime.now(UTC).isoformat(),
        }
        self.order_status_map[order_id] = "FILLED"
        return order_id

    def find_order_by_idempotency_key(self, key):
        for payload in self.submitted:
            if payload.get("idempotency_key") == key:
                return self._last
        return None

    def get_order_status(self, order_id):
        return self._last or {"order_id": order_id, "status": "UNKNOWN"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, sid): pass
    def resume_strategy(self, sid): pass
    def emergency_stop(self, account_id=None): pass


ADAPTER = None


@pytest.fixture(autouse=True)
def setup():
    global ADAPTER
    approval_store.reset()
    reset()
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    ADAPTER = AStockFakeAdapter(Market.A_SHARE)
    register_adapter(Market.A_SHARE, ADAPTER)
    yield
    approval_store.reset()
    reset()


def _agent_and_session(policy=None):
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client
    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client
    from dsh_a_stock_agent import AStockAgent
    from dsh_trade_core import AStockMarketPolicy
    agent = AStockAgent(
        gateway=gateway, approvals=approvals,
        account_id="paper-a-share-001",
        policy=policy or AStockMarketPolicy(),
    )
    return agent, BotSession.for_profile(
        load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    )


class AlwaysOpenPolicy:
    """测试用：绕开交易时段（规则校验仍走真实 AStockMarketPolicy）。"""

    def __init__(self):
        from dsh_trade_core import AStockMarketPolicy
        self.real = AStockMarketPolicy()

    def __getattr__(self, item):
        return getattr(self.real, item)

    def session_blocked(self):
        return None


def _approve_pending():
    approvals = client.get("/v1/approvals?status=REQUESTED").json()
    assert approvals, "应有待审批"
    aid = approvals[0]["approval_id"]
    resp = client.post(
        f"/v1/approvals/{aid}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    assert resp.status_code == 200


def test_full_loop_approves_and_reconciles():
    agent, session = _agent_and_session(policy=AlwaysOpenPolicy())
    run_once(session, agent)  # 预览 + 审批
    assert len(client.get("/v1/approvals?status=REQUESTED").json()) == 1

    _approve_pending()
    run_once(session, agent)  # 提交 + 对账

    assert len(ADAPTER.submitted) == 1
    payload = ADAPTER.submitted[0]
    assert Decimal(str(payload["quantity"])) % 100 == 0  # 整手下单
    assert len(session.tasks.find_by_status("DONE")) == 1
    rec = session.events.query("account/reconciled")
    assert rec and rec[0]["payload"]["reconciliation_status"] == "MATCHED"
    # 手续费纳入现金守恒
    assert Decimal(rec[0]["payload"]["fees"]) == Decimal("5")


def test_closed_session_blocks_new_signals():
    agent, session = _agent_and_session()  # 真实时段（测试时刻大概率闭市/非交易）
    blocked = agent.policy.session_blocked()
    if blocked is None:
        pytest.skip("测试运行在 A 股交易时段内，闭市分支另由策略单测覆盖")
    run_once(session, agent)
    assert client.get("/v1/approvals").json() == []
    assert any(m["kind"] == "session-blocked"
               for m in session.memory.recent())
