"""持久化、鉴权与审计测试。

验收标准（阶段一）：
- 重启后审批、幂等键、审计不丢失
- 重复请求不会重复下单（跨「重启」）
- 配置 API key 后未授权请求被拒绝
"""

import pytest
from fastapi.testclient import TestClient

from quant_gateway import approval_store, storage
from quant_gateway.main import app

client = TestClient(app)


def _persisted_db(tmp_path, monkeypatch):
    db = tmp_path / "gateway.db"
    monkeypatch.setenv("QUANT_GATEWAY_DB", str(db))
    approval_store.reset()  # 丢弃旧连接，下一次访问用新路径
    return db


def test_approvals_survive_restart(tmp_path, monkeypatch):
    _persisted_db(tmp_path, monkeypatch)
    created = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "order",
        "subject_id": "intent-1",
    }).json()

    # 模拟重启：丢弃内存连接，重新打开同一数据库
    approval_store.reset()
    fetched = client.get(f"/v1/approvals/{created['approval_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "REQUESTED"


def test_idempotency_survives_restart(tmp_path, monkeypatch):
    _persisted_db(tmp_path, monkeypatch)
    assert storage.record_idempotency_key("key-1", "ord-1")
    storage.reset()
    assert storage.get_order_id_for_key("key-1") == "ord-1"
    assert not storage.record_idempotency_key("key-1", "ord-2")  # 重复插入被拒


def test_audit_trail_recorded(tmp_path, monkeypatch):
    _persisted_db(tmp_path, monkeypatch)
    created = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "order",
        "subject_id": "intent-1",
    }).json()
    client.post(
        f"/v1/approvals/{created['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )

    entries = client.get("/v1/audit").json()
    actions = [e["action"] for e in entries]
    assert "approval.requested" in actions
    assert "approval.approved" in actions


AUTH_ENV = {"QUANT_GATEWAY_API_KEYS": "chief-secret/alice:read,write;viewer-secret/bob:read"}


def test_auth_rejects_missing_or_invalid_key(monkeypatch):
    for k, v in AUTH_ENV.items():
        monkeypatch.setenv(k, v)
    assert client.get("/v1/markets/A_SHARE/positions").status_code == 401
    assert client.get(
        "/v1/markets/A_SHARE/positions",
        headers={"X-API-Key": "wrong"},
    ).status_code == 401


def test_auth_scope_enforced(monkeypatch):
    for k, v in AUTH_ENV.items():
        monkeypatch.setenv(k, v)
    # read-only key 可以读
    assert client.get(
        "/v1/approvals", headers={"X-API-Key": "viewer-secret"}
    ).status_code == 200
    # read-only key 不能决定审批
    created = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "order",
        "subject_id": "intent-1",
    }, headers={"X-API-Key": "chief-secret"}).json()
    assert client.post(
        f"/v1/approvals/{created['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "bob"},
        headers={"X-API-Key": "viewer-secret"},
    ).status_code == 403
    # write key 可以决定
    assert client.post(
        f"/v1/approvals/{created['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
        headers={"X-API-Key": "chief-secret"},
    ).status_code == 200
