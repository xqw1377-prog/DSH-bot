"""strategy-evolution 持久化与门禁执行器测试。

覆盖：SQLite 账本持久化、实验结果记录、独立审计门禁（mock auditor）。
原有状态机测试见 test_state_machine.py。
"""

import pytest
from fastapi.testclient import TestClient

from strategy_evolution import storage
from strategy_evolution.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_storage(monkeypatch):
    storage.reset()
    yield
    storage.reset()


def _create_experiment(market="CRYPTO", strategy_id="trend-1") -> str:
    resp = client.post("/v1/experiments", json={
        "market": market,
        "strategy_id": strategy_id,
        "hypothesis": "动量在低波动率环境表现更好",
        "data_snapshot_id": "snap-1",
    })
    assert resp.status_code == 201
    return resp.json()["experiment_id"]


def _create_candidate(market="CRYPTO", strategy_id="trend-1",
                      version="1.0.0") -> str:
    resp = client.post("/v1/candidates", json={
        "market": market,
        "strategy_id": strategy_id,
        "strategy_version": version,
    })
    assert resp.status_code == 201
    return resp.json()["candidate_id"]


def _advance_to_shadow(cand_id: str) -> None:
    for stage, refs in [
        ("BACKTESTED", ["b1"]),
        ("VALIDATED", ["b1", "p1"]),
        ("PAPER", ["b1", "p1", "s1"]),
        ("SHADOW", ["b1", "p1", "s1"]),
    ]:
        r = client.post(f"/v1/candidates/{cand_id}/promote", json={
            "target_stage": stage, "evidence_refs": refs,
        })
        assert r.status_code == 200, r.text


def test_experiments_persisted():
    exp_id = _create_experiment()
    got = client.get(f"/v1/experiments/{exp_id}").json()
    assert got["experiment_id"] == exp_id
    items = client.get("/v1/experiments").json()
    assert any(i["experiment_id"] == exp_id for i in items)


def test_experiments_filtered_by_market():
    _create_experiment(market="CRYPTO", strategy_id="c1")
    _create_experiment(market="A_SHARE", strategy_id="a1")
    crypto = client.get("/v1/experiments?market=CRYPTO").json()
    a_share = client.get("/v1/experiments?market=A_SHARE").json()
    assert all(e["market"] == "CRYPTO" for e in crypto)
    assert all(e["market"] == "A_SHARE" for e in a_share)
    assert len(crypto) == 1 and len(a_share) == 1


def test_record_result_completes_experiment():
    exp_id = _create_experiment()
    resp = client.post(f"/v1/experiments/{exp_id}/result", json={
        "result_ref": "backtest-2026-01"
    })
    assert resp.status_code == 200
    assert resp.json()["result_ref"] == "backtest-2026-01"
    assert resp.json()["status"] == "COMPLETED"


def test_record_result_404_for_unknown():
    resp = client.post("/v1/experiments/nope/result", json={"result_ref": "r"})
    assert resp.status_code == 404


def test_candidates_persisted_across_stages():
    cand_id = _create_candidate()
    refs = ["backtest:1", "paper:2"]
    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "BACKTESTED", "evidence_refs": refs[:1],
    })
    assert r.status_code == 200
    got = client.get(f"/v1/candidates/{cand_id}").json()
    assert got["stage"] == "BACKTESTED"


def test_promotion_rejects_when_gate_not_satisfied():
    cand_id = _create_candidate()
    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "VALIDATED", "evidence_refs": ["only-one"],
    })
    assert r.status_code == 422


def test_promotion_to_approved_requires_auditor_url(monkeypatch):
    """未配置 auditor URL 时 APPROVED 晋级必须失败关闭。"""
    monkeypatch.setattr("strategy_evolution.main.AUDITOR_URL", "")
    cand_id = _create_candidate()
    _advance_to_shadow(cand_id)
    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "APPROVED",
        "evidence_refs": ["b1", "p1", "s1"],
        "approval_id": "appr-1",
    })
    assert r.status_code == 503
    assert "AUDITOR_URL" in r.json()["detail"]


def test_independent_audit_gate_rejects_promotion(monkeypatch):
    """配置 auditor URL 但 mock 其返回 rejected → APPROVED 晋级被拒。"""
    from strategy_evolution import main as se_main
    monkeypatch.setattr(se_main, "AUDITOR_URL", "http://auditor.test")

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "audit_id": "aud-1",
                "approved": False,
                "reason": "rejected",
                "evidence_hash": "abc",
            }

    monkeypatch.setattr(se_main.httpx, "post", lambda *a, **k: FakeResp())

    cand_id = _create_candidate()
    _advance_to_shadow(cand_id)

    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "APPROVED",
        "evidence_refs": ["b1", "p1", "s1"],
        "approval_id": "appr-1",
    })
    assert r.status_code == 422
    assert "independent audit rejected" in r.json()["detail"]


def test_independent_audit_gate_passes_when_approved(monkeypatch):
    from strategy_evolution import main as se_main
    monkeypatch.setattr(se_main, "AUDITOR_URL", "http://auditor.test")

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "audit_id": "aud-ok",
                "approved": True,
                "reason": "ok",
                "evidence_hash": "hash-1",
                "strategy_version": "1.0.0",
            }

    monkeypatch.setattr(se_main.httpx, "post", lambda *a, **k: FakeResp())

    cand_id = _create_candidate()
    _advance_to_shadow(cand_id)

    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "APPROVED",
        "evidence_refs": ["b1", "p1", "s1"],
        "approval_id": "appr-1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "APPROVED"
    assert any(ref.startswith("audit:aud-ok:") for ref in body["evidence_refs"])


def test_audit_failure_is_fail_closed(monkeypatch):
    """auditor 不可达 → 失败关闭，晋级被拒。"""
    from strategy_evolution import main as se_main
    monkeypatch.setattr(se_main, "AUDITOR_URL", "http://auditor.test")
    monkeypatch.setattr(
        se_main.httpx, "post",
        lambda *a, **k: (_ for _ in ()).throw(se_main.httpx.ConnectError("down")),
    )

    cand_id = _create_candidate()
    _advance_to_shadow(cand_id)

    r = client.post(f"/v1/candidates/{cand_id}/promote", json={
        "target_stage": "APPROVED",
        "evidence_refs": ["b1", "p1", "s1"],
        "approval_id": "appr-1",
    })
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]
