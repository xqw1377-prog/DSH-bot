"""故障演练：断网、重复请求、部分成交、UNKNOWN、风控不可用、事故中心不可用。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
)
from dsh_crypto_agent import CryptoAgent
from dsh_gateway_client import GatewayClient, GatewayError
from dsh_market_chief import MarketChiefAgent
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from fastapi.testclient import TestClient
from quant_gateway import approval_store
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"
client = TestClient(app)


class DrillAdapter(MarketAdapter):
    def __init__(self):
        self.market = Market.CRYPTO
        self.submitted: list[dict] = []
        self.next_submit_status = "FILLED"
        self._qty = Decimal("0")
        self._cash = Decimal("50000")
        self._price = Decimal("100")
        self._last_order_id = ""

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        from dsh_contracts import Position
        return [Position(
            market=self.market, account_id="crypto-paper-1",
            symbol="BTCUSDT", quantity=self._qty, available_quantity=self._qty,
            frozen_quantity=Decimal("0"),
            avg_cost=self._price, currency="USDT", as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="crypto-paper-1",
            cash=self._cash, equity=self._cash + self._qty * self._price,
            currency="USDT", reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        now = datetime.now(UTC)
        return [Signal(
            signal_id="drill-sig", market=self.market,
            strategy_id="momentum", strategy_version="0.1.0",
            symbol="BTCUSDT", side="BUY", strength=0.9,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-drill",
        )]

    def preview_order(self, intent):
        qty = Decimal(str(intent["quantity"])) if isinstance(intent, dict) else intent.quantity
        notional = qty * self._price
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.0005"),
            risk=RiskSnapshot(
                risk_snapshot_id="rs-drill", market=self.market,
                account_id="crypto-paper-1",
                position_before=self._qty, position_after=self._qty + qty,
                risk_budget_delta=notional,
                worst_case_loss=notional * Decimal("0.01"),
                limits_hit=[], as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def find_order_by_idempotency_key(self, key):
        for payload in self.submitted:
            if payload.get("idempotency_key") == key:
                return {"order_id": self._last_order_id}
        return None

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"CRYPTO-drill-{len(self.submitted)}"
        self._last_order_id = order_id
        qty = Decimal(str(payload.get("quantity", "0.01")))
        if self.next_submit_status == "FILLED":
            self._qty += qty
            self._cash -= qty * self._price
        return order_id

    def get_order_status(self, order_id):
        filled = "0.005" if self.next_submit_status == "PARTIALLY_FILLED" else "0.01"
        return {
            "order_id": order_id, "status": self.next_submit_status,
            "filled_quantity": filled, "avg_price": str(self._price),
            "fees": "0", "taxes": "0",
            "fills": [{"quantity": filled, "price": str(self._price), "fee": "0"}],
        }

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, sid):
        pass

    def resume_strategy(self, sid):
        pass

    def emergency_stop(self, account_id=None):
        pass


ADAPTER = None


def setup_function():
    global ADAPTER
    approval_store.reset()
    reset()
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    ADAPTER = DrillAdapter()
    register_adapter(Market.CRYPTO, ADAPTER)
    register_adapter(Market.A_SHARE, DrillAdapter())


def teardown_function():
    approval_store.reset()
    reset()


def _agent():
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client
    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client
    agent = CryptoAgent(
        gateway=gateway, approvals=approvals, account_id="crypto-paper-1",
    )
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    return agent, session


def _approve(session, agent):
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "drill"},
    )
    return task


def test_disconnect_before_venue_is_submission_unknown():
    agent, session = _agent()
    _approve(session, agent)

    def boom(*_a, **_k):
        raise GatewayError(503, "network down", submission_unknown=True)

    agent.gateway.request_order = boom  # type: ignore[method-assign]
    run_once(session, agent)
    assert session.tasks.find_by_status("SUBMISSION_UNKNOWN")
    assert ADAPTER.submitted == []


def test_duplicate_request_single_venue_order():
    agent, session = _agent()
    _approve(session, agent)
    run_once(session, agent)
    run_once(session, agent)
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert session.tasks.find_by_status("DONE")


def test_partial_fill_does_not_resubmit():
    agent, session = _agent()
    _approve(session, agent)
    ADAPTER.next_submit_status = "PARTIALLY_FILLED"
    run_once(session, agent)
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert session.tasks.find_by_status("PARTIALLY_FILLED")


def test_unknown_does_not_resubmit():
    agent, session = _agent()
    _approve(session, agent)
    ADAPTER.next_submit_status = "UNKNOWN"
    run_once(session, agent)
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert session.tasks.find_by_status("SUBMITTED")


def test_risk_policy_unavailable_blocks_pre_submit():
    agent, session = _agent()
    _approve(session, agent)

    def down(*_a, **_k):
        raise RuntimeError("risk-policy down")

    orders_router.check_order_risk = down
    run_once(session, agent)
    assert ADAPTER.submitted == []
    blocked = session.tasks.find_by_status("PRE_SUBMIT_BLOCKED")
    failed = session.tasks.find_by_status("PRE_SUBMIT_FAILED")
    assert blocked or failed


def test_incident_center_down_does_not_break_chief():
    import os

    os.environ["INCIDENT_CENTER_URL"] = "http://127.0.0.1:9"
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client
    agent = MarketChiefAgent(gateway=gateway)
    session = BotSession.for_profile(
        load_profile(PROFILES / "market-chief" / "profile.yaml")
    )
    run_once(session, agent)
    assert session.events.query("market/chief.summary")
    os.environ.pop("INCIDENT_CENTER_URL", None)
