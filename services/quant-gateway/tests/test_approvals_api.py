from dsh_contracts import ApprovalStatus, Market
from fastapi.testclient import TestClient

from quant_gateway.main import app


def test_approval_lifecycle():
    client = TestClient(app)
    create = client.post("/v1/approvals", json={
        "market": "A_SHARE",
        "requested_by_bot": "a-stock-bot",
        "subject_type": "order",
        "subject_id": "sub-9",
        "evidence_refs": ["sig-1", "risk-1"],
        "binding": {
            "market": "A_SHARE",
            "account_id": "acc-1",
            "symbol": "600519.SH",
            "side": "BUY",
            "quantity": "100",
            "strategy_version": "0.1.0",
            "signal_snapshot_id": "sig-9",
            "risk_snapshot_id": "risk-9",
            "valid_until": "2099-01-01T00:00:00Z",
        },
    })
    assert create.status_code == 201
    approval = create.json()
    assert approval["status"] == "REQUESTED"
    assert approval["intent_digest"] is not None

    listed = client.get("/v1/approvals", params={"status": "REQUESTED"}).json()
    assert any(a["approval_id"] == approval["approval_id"] for a in listed)

    decided = client.post(
        f"/v1/approvals/{approval['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "human"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"
    assert decided.json()["decided_by"] == "human"


def test_order_approval_requires_binding():
    client = TestClient(app)
    resp = client.post("/v1/approvals", json={
        "market": "A_SHARE",
        "requested_by_bot": "a-stock-bot",
        "subject_type": "order",
        "subject_id": "sub-b",
    })
    assert resp.status_code == 422
    assert "binding" in resp.json()["detail"]

    unknown = client.post("/v1/approvals", json={
        "market": "A_SHARE",
        "requested_by_bot": "a-stock-bot",
        "subject_type": "order",
        "subject_id": "sub-b",
        "binding": {"market": "A_SHARE", "symbol": "600519.SH", "bogus": "x"},
    })
    assert unknown.status_code == 422
    assert "unexpected binding fields" in unknown.json()["detail"]

    ok = client.post("/v1/approvals", json={
        "market": "A_SHARE",
        "requested_by_bot": "a-stock-bot",
        "subject_type": "order",
        "subject_id": "sub-b",
        "binding": {
            "market": "A_SHARE", "symbol": "600519.SH", "side": "BUY",
            "quantity": "100",
        },
    })
    assert ok.status_code == 201


def test_non_order_approval_needs_no_binding():
    client = TestClient(app)
    resp = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "control_action",
        "subject_id": "pause-strat-2",
    })
    assert resp.status_code == 201
    assert resp.json()["intent_digest"] is None


def test_decision_cannot_be_flipped():
    client = TestClient(app)
    approval = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "control_action",
        "subject_id": "pause-strat-2",
    }).json()
    approval_id = approval["approval_id"]
    assert client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "REJECTED", "decided_by": "human"},
    ).status_code == 200
    resp = client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "APPROVED", "decided_by": "human"},
    )
    assert resp.status_code == 422


def test_unknown_approval_returns_404():
    client = TestClient(app)
    assert client.get("/v1/approvals/appr-none").status_code == 404
