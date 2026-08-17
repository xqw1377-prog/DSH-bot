"""A 股复用 Crypto 异常矩阵：404、拒单、部分成交、UNKNOWN、重启、对账、Shadow。"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from dsh_a_stock_agent import AShareAgent
from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
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


class MatrixAdapter(MarketAdapter):
    def __init__(self, market: Market):
        self.market = market
        self.submitted: list[dict] = []
        self.order_status: dict[str, str] = {}
        self.next_submit_status = "FILLED"
        self.next_submit_error: Exception | None = None
        self._qty = Decimal("0")
        self._cash = Decimal("1000000")
        self._price = Decimal("1680.50")
        self._last_order_id = ""

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        from dsh_contracts import Position
        return [Position(
            market=self.market, account_id="paper-a-share-001",
            symbol="600519", quantity=self._qty, available_quantity=self._qty,
            frozen_quantity=Decimal("0"),
            avg_cost=self._price, currency="CNY", as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="paper-a-share-001",
            cash=self._cash, equity=self._cash + self._qty * self._price,
            available_cash=self._cash, frozen_cash=Decimal("0"),
            currency="CNY", reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        now = datetime.now(UTC)
        return [Signal(
            signal_id="ashare-sig-x", market=self.market,
            strategy_id="mean-reversion-ashare", strategy_version="0.1.0",
            symbol="600519", side="BUY", strength=0.8,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-a",
        )]

    def preview_order(self, intent):
        qty = Decimal(str(intent["quantity"])) if isinstance(intent, dict) else intent.quantity
        notional = qty * self._price
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.0005"),
            risk=RiskSnapshot(
                risk_snapshot_id="rs-ashare-sig-x", market=self.market,
                account_id="paper-a-share-001",
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
        if self.next_submit_error is not None:
            raise self.next_submit_error
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"{self.market.value}-ord-{len(self.submitted)}"
        self._last_order_id = order_id
        self.order_status[order_id] = self.next_submit_status
        qty = Decimal(str(payload.get("quantity", "0.01")))
        if self.next_submit_status == "FILLED":
            self._qty += qty
            self._cash -= qty * self._price
        return order_id

    def get_order_status(self, order_id):
        status = self.order_status.get(order_id, "UNKNOWN")
        filled = "0.005" if status == "PARTIALLY_FILLED" else "0.01"
        return {
            "order_id": order_id, "status": status,
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


@pytest.fixture(autouse=True)
def setup():
    global ADAPTER
    approval_store.reset()
    reset()
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    ADAPTER = MatrixAdapter(Market.A_SHARE)
    register_adapter(Market.A_SHARE, ADAPTER)
    register_adapter(Market.CRYPTO, MatrixAdapter(Market.CRYPTO))
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
    agent = AShareAgent(
        gateway=gateway, approvals=approvals, account_id="paper-a-share-001",
    )
    return agent, BotSession.for_profile(
        load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    )


def _drive_to_approved(session, agent):
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    resp = client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    assert resp.status_code == 200
    return task


def test_ashare_approval_404_goes_unknown():
    agent, session = _agent_and_session()
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    from quant_gateway import storage
    with storage.locked_conn() as conn:
        conn.execute(
            "DELETE FROM approvals WHERE approval_id = ?", (task["approval_id"],)
        )
        conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert session.tasks.find_by_status("APPROVAL_UNKNOWN")


def test_ashare_venue_reject():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.next_submit_status = "REJECTED"
    run_once(session, agent)
    assert ADAPTER.submitted
    assert session.tasks.find_by_status("ORDER_REJECTED")


def test_ashare_partial_fill():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.next_submit_status = "PARTIALLY_FILLED"
    run_once(session, agent)
    assert session.tasks.find_by_status("PARTIALLY_FILLED")
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1


def test_ashare_unknown_stays_in_flight():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.next_submit_status = "UNKNOWN"
    run_once(session, agent)
    assert session.tasks.find_by_status("SUBMITTED")
    assert len(ADAPTER.submitted) == 1
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1


def test_ashare_restart_does_not_double_submit():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING', order_id = NULL"
        " WHERE bot = 'a-stock-bot'"
    )
    conn.commit()
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert session.tasks.find_by_status("DONE")


def test_ashare_strict_reconcile_mismatch_opens_incident():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)
    done = session.tasks.find_by_status("DONE")
    if done:
        from dsh_runtime.store import _get
        conn = _get()
        conn.execute(
            "UPDATE bot_tasks SET status = 'FILLED', reconciliation_status = 'PENDING'"
            " WHERE bot = 'a-stock-bot'"
        )
        conn.commit()
        ADAPTER._qty = Decimal("0")
        run_once(session, agent)
        incidents = session.events.query("incident/opened")
        assert incidents or session.tasks.find_by_status("INCIDENT")


def test_ashare_shadow_zero_writes():
    agent, session = _agent_and_session()
    agent.mode = "shadow"
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert session.tasks.find_by_status("SHADOW_RECORDED")
