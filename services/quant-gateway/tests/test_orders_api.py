from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from dsh_contracts import (
    AccountSummary,
    ApprovalStatus,
    Market,
    RiskSnapshot,
)
from fastapi.testclient import TestClient

from quant_gateway import approval_store
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router
from quant_gateway.routers.orders import register_risk_snapshot


@pytest.fixture()
def client():
    # FakeAdapter 已由 conftest 的 reset_gateway_state 自动注册
    return TestClient(app)


@pytest.fixture()
def risk_pass(monkeypatch):
    """让二次硬风控直接通过（risk-policy 的规则另由其自身测试覆盖）。"""
    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {"passed": True, "limits_hit": []},
    )


@pytest.fixture()
def risk_reject(monkeypatch):
    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {"passed": False, "limits_hit": ["max_position"]},
    )


def approved_approval() -> str:
    approval = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-1",
    )
    return approval_store.decide_approval(
        approval.approval_id, ApprovalStatus.APPROVED, "human"
    ).approval_id


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


def test_order_rejected_without_risk_snapshot(client, risk_pass):
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    assert "fail-closed" in resp.json()["detail"]


def test_order_rejected_when_limits_hit(client, risk_pass):
    register_risk_snapshot(make_snapshot(limits_hit=["max_position"]))
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422


def test_order_rejected_when_risk_policy_rejects(client, risk_reject):
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    assert "risk check failed" in resp.json()["detail"]


def test_order_rejected_when_risk_policy_unreachable(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise ConnectionError("risk-policy down")

    monkeypatch.setattr(orders_router, "check_order_risk", unreachable)
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 503
    assert "fail-closed" in resp.json()["detail"]


def test_order_rejected_without_approved_approval(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    pending = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-1",
    ).approval_id
    resp = client.post(
        "/v1/markets/A_SHARE/orders", json=make_intent(approval_id=pending)
    )
    assert resp.status_code == 422


def test_order_submitted_when_all_gates_pass(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(approval_id=approved_approval()),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_idempotency_replay_rejected(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="dup-key", approval_id=approved_approval())
    assert client.post("/v1/markets/A_SHARE/orders", json=body).status_code == 200
    resp = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert resp.status_code == 409


def test_intent_validation_rejects_fuzzy_body(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="bad-1", quantity=None),
    )
    assert resp.status_code == 422
