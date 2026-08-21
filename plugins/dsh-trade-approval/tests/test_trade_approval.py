"""审批工作流插件测试：直接对 Quant Gateway 应用做集成测试。"""

import pytest
from fastapi.testclient import TestClient

from dsh_trade_approval import ApprovalWorkflow
from quant_gateway.main import app


@pytest.fixture()
def workflow():
    # 用 ASGITransport 直连网关应用，不占用端口
    wf = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(
        wf, gateway_base_url="http://testserver", timeout=5.0
    )
    wf._client = TestClient(app)  # TestClient 兼容 httpx Client 接口子集
    return wf


BINDING = {
    "market": "CRYPTO",
    "account_id": "paper-crypto-001",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "order_type": "MARKET",
    "quantity": "0.01",
    "strategy_version": "1",
    "signal_snapshot_id": "signal-1",
    "risk_snapshot_id": "risk-snap-1",
    "valid_until": "2099-01-01T00:00:00Z",
}


def test_request_creates_pending_approval(workflow):
    approval_id = workflow.request(
        market="CRYPTO",
        requested_by_bot="crypto-bot",
        subject_type="order",
        subject_id="intent-42",
        evidence_refs=["signal-1", "risk-snap-1"],
        binding=BINDING,
    )
    assert approval_id.startswith("appr-")
    fetched = workflow._client.get(f"/v1/approvals/{approval_id}").json()
    assert fetched["status"] == "REQUESTED"
    assert fetched["evidence_refs"] == ["signal-1", "risk-snap-1"]


def test_wait_returns_approved_after_human_decision(workflow):
    approval_id = workflow.request(
        "CRYPTO", "crypto-bot", "order", "intent-43", binding=BINDING
    )
    # 人工在前端批准
    workflow._client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    outcome = workflow.wait_for_decision(approval_id, poll_interval=0.01, max_wait_seconds=1)
    assert outcome.approved
    assert outcome.approval_id == approval_id


def test_wait_returns_rejected(workflow):
    approval_id = workflow.request(
        "CRYPTO", "crypto-bot", "order", "intent-44", binding=BINDING
    )
    workflow._client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "REJECTED", "decided_by": "alice"},
    )
    outcome = workflow.wait_for_decision(approval_id, poll_interval=0.01, max_wait_seconds=1)
    assert outcome.status == "REJECTED"
    assert not outcome.approved


def test_wait_times_out_fail_closed(workflow):
    approval_id = workflow.request(
        "CRYPTO", "crypto-bot", "order", "intent-45", binding=BINDING
    )
    outcome = workflow.wait_for_decision(
        approval_id, poll_interval=0.01, max_wait_seconds=0.05
    )
    assert outcome.status == "TIMEOUT"
    assert not outcome.approved


def test_workflow_module_has_no_decide_api(workflow):
    """红线：插件不允许暴露替人决定的接口。"""
    forbidden = {"decide", "approve", "reject", "decide_approval"}
    public = {n for n in dir(ApprovalWorkflow) if not n.startswith("_")}
    assert not (public & forbidden)
