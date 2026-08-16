from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from dsh_contracts import ApprovalStatus, Market, OrderSide, RiskSnapshot
from fastapi.testclient import TestClient

from quant_gateway.main import app
from quant_gateway.routers.orders import register_approval, register_risk_snapshot


@pytest.fixture()
def client():
    from quant_gateway.adapters import register_adapter
    from quant_gateway.adapters.base import MarketAdapter

    class StubAdapter(MarketAdapter):
        def get_health(self): ...
        def get_positions(self, account_id=None): return []
        def get_account_summary(self): return []
        def get_signals(self): return []
        def preview_order(self, intent): ...
        def request_order(self, intent): return f"order-{intent['idempotency_key']}"
        def get_order_status(self, order_id): return {"order_id": order_id}
        def cancel_order(self, order_id): return {"order_id": order_id, "status": "CANCELLED"}
        def pause_strategy(self, strategy_id): ...
        def resume_strategy(self, strategy_id): ...
        def emergency_stop(self, account_id=None): ...

    register_adapter(Market.A_SHARE, StubAdapter())
    return TestClient(app)


def make_intent(**overrides) -> dict:
    intent = {
        "idempotency_key": "key-1",
        "market": "A_SHARE",
        "account_id": "acc-1",
        "strategy_id": "strat-1",
        "strategy_version": "0.1.0",
        "symbol": "600519.SH",
        "side": "BUY",
        "quantity": "100",
        "valid_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "signal_snapshot_id": "sig-1",
        "risk_snapshot_id": "risk-1",
        "approval_id": "appr-1",
    }
    intent.update(overrides)
    return intent


def make_snapshot(**overrides) -> RiskSnapshot:
    base = dict(
        risk_snapshot_id="risk-1",
        market=Market.A_SHARE,
        account_id="acc-1",
        position_before=Decimal("0"),
        position_after=Decimal("100"),
        risk_budget_delta=Decimal("10000"),
        worst_case_loss=Decimal("500"),
        as_of=datetime.now(UTC),
    )
    base.update(overrides)
    return RiskSnapshot(**base)


def test_order_rejected_without_risk_snapshot(client):
    register_approval("appr-1", ApprovalStatus.APPROVED)
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    assert "fail-closed" in resp.json()["detail"]


def test_order_rejected_when_limits_hit(client):
    register_risk_snapshot(make_snapshot(limits_hit=["max_position"]))
    register_approval("appr-1", ApprovalStatus.APPROVED)
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422


def test_order_rejected_without_approved_approval(client):
    register_risk_snapshot(make_snapshot())
    register_approval("appr-1", ApprovalStatus.REQUESTED)
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422


def test_order_submitted_when_all_gates_pass(client):
    register_risk_snapshot(make_snapshot())
    register_approval("appr-2", ApprovalStatus.APPROVED)
    resp = client.post(
        "/v1/markets/A_SHARE/orders", json=make_intent(approval_id="appr-2")
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_idempotency_replay_rejected(client):
    register_risk_snapshot(make_snapshot())
    register_approval("appr-3", ApprovalStatus.APPROVED)
    body = make_intent(idempotency_key="dup-key", approval_id="appr-3")
    assert client.post("/v1/markets/A_SHARE/orders", json=body).status_code == 200
    resp = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert resp.status_code == 409


def test_intent_validation_rejects_fuzzy_body(client):
    register_risk_snapshot(make_snapshot())
    register_approval("appr-4", ApprovalStatus.APPROVED)
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="bad-1", approval_id="appr-4", quantity=None),
    )
    assert resp.status_code == 422
