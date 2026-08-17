"""Risk Auditor HTTP 服务测试。"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from dsh_contracts import StrategyCandidate, StrategyStage
from dsh_risk_auditor import storage
from dsh_risk_auditor.service import app

client = TestClient(app)


def setup_function():
    storage.reset()


def teardown_function():
    storage.reset()


def _candidate() -> dict:
    return StrategyCandidate(
        candidate_id="cand-1",
        market="CRYPTO",
        strategy_id="s-1",
        strategy_version="1.2.0",
        stage=StrategyStage.SHADOW,
        updated_at=datetime.now(UTC),
    ).model_dump(mode="json")


def test_health():
    assert client.get("/health").json()["service"] == "risk-auditor"
    assert client.get("/healthz").json()["status"] == "ok"


def test_audit_promotion_rejects_homogeneous_evidence():
    resp = client.post("/v1/audit-promotion", json={
        "candidate": _candidate(),
        "evidence_refs": ["backtest:1", "backtest:2", "backtest:3"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] is False
    assert body["audit_id"]
    assert body["strategy_version"] == "1.2.0"
    assert body["evidence_hash"]
    assert "同源" in body["reason"]
    # 持久化可查
    got = client.get(f"/v1/audits/{body['audit_id']}").json()
    assert got["audit_id"] == body["audit_id"]


def test_audit_promotion_passes_with_diverse_evidence():
    resp = client.post("/v1/audit-promotion", json={
        "candidate": _candidate(),
        "evidence_refs": ["backtest:1", "paper:2", "shadow:3"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] is True
    assert body["strategy_id"] == "s-1"
    assert len(body["evidence_hash"]) == 64
