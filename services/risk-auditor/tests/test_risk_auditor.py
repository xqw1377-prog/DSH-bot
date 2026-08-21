"""Risk Auditor 测试：正反例、幂等结论、哈希防篡改、重启恢复。"""

import pytest
from fastapi.testclient import TestClient

from risk_auditor import storage
from risk_auditor.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean():
    storage.reset()
    yield
    storage.reset()


def _req(**over):
    refs = ["ev-1", "ev-2", "ev-3"]
    base = dict(
        candidate_id="cand-1", market="CRYPTO", strategy_id="s",
        strategy_version="1.0.0", from_stage="SHADOW", to_stage="APPROVED",
        evidence_refs=refs, evidence_hash=storage.evidence_hash(refs),
        approval_id="appr-1",
    )
    base.update(over)
    return base


def test_pass_with_valid_evidence_and_approval():
    r = client.post("/v1/audit-promotion", json=_req())
    assert r.status_code == 200
    assert r.json()["verdict"] == "PASS"
    assert r.json()["idempotent"] is False


def test_fail_on_hash_mismatch():
    bad = _req(evidence_hash="deadbeef")
    r = client.post("/v1/audit-promotion", json=bad)
    assert r.status_code == 200
    assert r.json()["verdict"] == "FAIL"
    assert "hash mismatch" in r.json()["reason"]


def test_fail_on_insufficient_evidence():
    refs = ["ev-1"]
    r = client.post("/v1/audit-promotion", json=_req(
        evidence_refs=refs, evidence_hash=storage.evidence_hash(refs)))
    assert r.json()["verdict"] == "FAIL"
    assert "insufficient" in r.json()["reason"]


def test_fail_without_approval():
    r = client.post("/v1/audit-promotion", json=_req(approval_id=None))
    assert r.json()["verdict"] == "FAIL"
    assert "approval" in r.json()["reason"]


def test_conclusion_idempotent():
    first = client.post("/v1/audit-promotion", json=_req()).json()
    second = client.post("/v1/audit-promotion", json=_req()).json()
    assert first["conclusion_id"] == second["conclusion_id"]
    assert second["idempotent"] is True


def test_conclusion_survives_restart(tmp_path, monkeypatch):
    db = tmp_path / "auditor.db"
    monkeypatch.setenv("RISK_AUDITOR_DB", str(db))
    storage.reset()
    first = client.post("/v1/audit-promotion", json=_req()).json()
    storage.reset()  # 模拟重启（文件库重开）
    again = client.post("/v1/audit-promotion", json=_req()).json()
    assert again["conclusion_id"] == first["conclusion_id"]
    assert again["idempotent"] is True


def test_fail_on_single_sourced_evidence():
    refs = ["backtest:a", "backtest:b", "backtest:c"]
    r = client.post("/v1/audit-promotion", json=_req(
        evidence_refs=refs, evidence_hash=storage.evidence_hash(refs)))
    assert r.json()["verdict"] == "FAIL"
    assert "single-sourced" in r.json()["reason"]


def test_pass_with_mixed_source_evidence():
    refs = ["backtest:a", "shadow:b", "paper:c"]
    r = client.post("/v1/audit-promotion", json=_req(
        evidence_refs=refs, evidence_hash=storage.evidence_hash(refs)))
    assert r.json()["verdict"] == "PASS"
