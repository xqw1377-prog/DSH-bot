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


def test_expired_approval_returns_expired_not_404():
    """过期审批返回 200 + status=EXPIRED，不返回 404。

    P0 修复：调用方能区分「审批不存在」与「审批已过期」。
    """
    import json
    from datetime import UTC, datetime, timedelta

    client = TestClient(app)
    create = client.post("/v1/approvals", json={
        "market": "A_SHARE",
        "requested_by_bot": "a-stock-bot",
        "subject_type": "order",
        "subject_id": "sub-exp",
    })
    approval_id = create.json()["approval_id"]

    # 模拟审批已过期：修改 payload 内的 requested_at 为 31 分钟前
    from quant_gateway import storage
    from quant_gateway.approval_store import _COLUMNS, _row_to_approval

    with storage.locked_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    approval = _row_to_approval(row)
    old_time = datetime.now(UTC) - timedelta(minutes=31)
    approval = approval.model_copy(update={"requested_at": old_time})
    with storage.locked_conn() as conn:
        conn.execute(
            "UPDATE approvals SET payload = ?, requested_at = ? WHERE approval_id = ?",
            (approval.model_dump_json(), old_time.isoformat(), approval_id),
        )
        conn.commit()

    # 获取审批：应该返回 200 + status=EXPIRED，而非 404
    resp = client.get(f"/v1/approvals/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "EXPIRED"

    # 列表查询 status=EXPIRED 应该能找到
    expired_list = client.get("/v1/approvals", params={"status": "EXPIRED"}).json()
    assert any(a["approval_id"] == approval_id for a in expired_list)

    # 决定过期审批应返回 422（已决），不是 404（不存在）
    decide_resp = client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "APPROVED", "decided_by": "human"},
    )
    assert decide_resp.status_code == 422
    assert "EXPIRED" in decide_resp.json()["detail"]
