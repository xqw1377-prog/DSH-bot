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
    })
    assert create.status_code == 201
    approval = create.json()
    assert approval["status"] == "REQUESTED"

    listed = client.get("/v1/approvals", params={"status": "REQUESTED"}).json()
    assert any(a["approval_id"] == approval["approval_id"] for a in listed)

    decided = client.post(
        f"/v1/approvals/{approval['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "human"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"
    assert decided.json()["decided_by"] == "human"


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
