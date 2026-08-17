"""治理闭环验收测试：持久化、幂等晋级、证据防篡改、乐观锁、
Auditor 失败关闭。"""

import pytest
from fastapi.testclient import TestClient

from strategy_evolution import storage
from strategy_evolution.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean():
    storage.reset()
    yield
    storage.reset()


def _make_candidate(market="CRYPTO") -> str:
    r = client.post("/v1/candidates", json={
        "market": market, "strategy_id": "momentum",
        "strategy_version": "1.0.0",
    })
    assert r.status_code == 201
    return r.json()["candidate_id"]


def _evidence(n):
    return [f"exp-{i}-report" for i in range(n)]


def _promote(cid, stage, refs, approval_id=None, version=None):
    return client.post(f"/v1/candidates/{cid}/promote", json={
        "target_stage": stage, "evidence_refs": refs,
        "approval_id": approval_id, "expected_version": version,
    })


def _drive_to_shadow(cid):
    """DRAFT→BACKTESTED→VALIDATED→PAPER→SHADOW（各阶段补证据）。"""
    steps = [("BACKTESTED", 1), ("VALIDATED", 2), ("PAPER", 2), ("SHADOW", 2)]
    for stage, n in steps:
        r = _promote(cid, stage, _evidence(n))
        assert r.status_code == 200, r.text


# ---- 1. 持久化与重启恢复 ----

def test_ledger_survives_restart(tmp_path, monkeypatch):
    db = tmp_path / "evolution.db"
    monkeypatch.setenv("STRATEGY_EVOLUTION_DB", str(db))
    storage.reset()

    cid = _make_candidate()
    exp = client.post("/v1/experiments", json={
        "market": "CRYPTO", "strategy_id": "momentum",
        "hypothesis": "动量延续", "data_snapshot_id": "snap-1",
    }).json()
    _drive_to_shadow(cid)

    storage.reset()  # 模拟进程重启（文件库重开）

    assert client.get(f"/v1/experiments/{exp['experiment_id']}").status_code == 200
    cand = client.get(f"/v1/candidates/{cid}").json()
    assert cand["stage"] == "SHADOW"
    assert cand["version"] == 5  # 创建 + 4 次晋级
    history = client.get(f"/v1/candidates/{cid}/audit-history").json()
    assert len([h for h in history if h["action"] == "promotion.applied"]) == 4


# ---- 2. 幂等晋级 ----

def test_idempotent_promotion_returns_same_state():
    cid = _make_candidate()
    _drive_to_shadow(cid)
    # 重放最后一次晋级（SHADOW 无需 Auditor，聚焦幂等语义本身）
    refs = _evidence(2)
    first = _promote(cid, "SHADOW", refs)
    assert first.status_code == 200
    version_after_first = client.get(f"/v1/candidates/{cid}").json()["version"]

    replay = _promote(cid, "SHADOW", refs)
    assert replay.status_code == 200
    version_after_replay = client.get(f"/v1/candidates/{cid}").json()["version"]
    assert version_after_replay == version_after_first  # 未二次迁移

    history = client.get(f"/v1/candidates/{cid}/audit-history").json()
    shadow_applied = [h for h in history
                      if h["action"] == "promotion.applied"
                      and h["to_stage"] == "SHADOW"]
    assert len(shadow_applied) == 1  # 审计历史只有一条


# ---- 3. 证据防篡改 ----

def test_evidence_tamper_fails_closed():
    cid = _make_candidate()
    _drive_to_shadow(cid)
    # 直接改库里的候选 payload evidence_refs（模拟篡改）
    with storage.locked_conn() as conn:
        import json as _json
        row = conn.execute(
            "SELECT payload FROM candidates WHERE candidate_id = ?",
            (cid,)).fetchone()
        payload = _json.loads(row[0])
        payload["evidence_refs"] = payload["evidence_refs"] + ["FAKE-REF"]
        conn.execute(
            "UPDATE candidates SET payload = ? WHERE candidate_id = ?",
            (_json.dumps(payload), cid))
        conn.commit()
    r = _promote(cid, "APPROVED", _evidence(3), approval_id="appr-1")
    assert r.status_code == 422
    assert "tampering" in r.json()["detail"]


# ---- 4. 乐观锁 ----

def test_version_conflict_rejected():
    cid = _make_candidate()
    r = _promote(cid, "BACKTESTED", _evidence(1), version=999)
    assert r.status_code == 409


# ---- 5. Auditor 失败关闭 ----

def _to_approved(cid):
    _drive_to_shadow(cid)
    return _promote(cid, "APPROVED", _evidence(3), approval_id="appr-1")


def test_auditor_unreachable_fails_closed(monkeypatch):
    monkeypatch.setenv("STRATEGY_EVOLUTION_AUDITOR_URL",
                       "http://127.0.0.1:1")  # 不可达端口
    import importlib
    client2 = client  # Auditor URL 调用时读取，无需重载模块
    resp = client2.post("/v1/candidates", json={
        "market": "CRYPTO", "strategy_id": "momentum",
        "strategy_version": "1.0.0"})
    cid = resp.json()["candidate_id"]
    for stage, n in [("BACKTESTED", 1), ("VALIDATED", 2),
                     ("PAPER", 2), ("SHADOW", 2)]:
        rr = client2.post(f"/v1/candidates/{cid}/promote", json={
            "target_stage": stage, "evidence_refs": _evidence(n)})
        assert rr.status_code == 200, rr.text
    rr = client2.post(f"/v1/candidates/{cid}/promote", json={
        "target_stage": "APPROVED", "evidence_refs": _evidence(3),
        "approval_id": "appr-1"})
    assert rr.status_code == 503
    assert "fail-closed" in rr.json()["detail"]
    # 状态未迁移
    assert client2.get(f"/v1/candidates/{cid}").json()["stage"] == "SHADOW"


def test_auditor_rejection_fails_closed(monkeypatch):
    """Auditor 明确驳回（证据不足）→ 422 拒绝晋级。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as TC

    fake = FastAPI()

    @fake.post("/v1/audit-promotion")
    def reject(payload: dict):
        return {"verdict": "FAIL", "reason": "insufficient independent evidence",
                "conclusion_id": "audit-x"}

    import threading, uvicorn
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=8095,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time
    for _ in range(40):
        try:
            import httpx
            httpx.get("http://127.0.0.1:8095/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.25)

    monkeypatch.setenv("STRATEGY_EVOLUTION_AUDITOR_URL",
                       "http://127.0.0.1:8095")
    import importlib
    client2 = client  # Auditor URL 调用时读取，无需重载模块
    cid = client2.post("/v1/candidates", json={
        "market": "CRYPTO", "strategy_id": "momentum",
        "strategy_version": "1.0.0"}).json()["candidate_id"]
    for stage, n in [("BACKTESTED", 1), ("VALIDATED", 2),
                     ("PAPER", 2), ("SHADOW", 2)]:
        assert client2.post(f"/v1/candidates/{cid}/promote", json={
            "target_stage": stage, "evidence_refs": _evidence(n)}).status_code == 200
    rr = client2.post(f"/v1/candidates/{cid}/promote", json={
        "target_stage": "APPROVED", "evidence_refs": _evidence(3),
        "approval_id": "appr-1"})
    assert rr.status_code == 422
    assert "verdict FAIL" in rr.json()["detail"] or "FAIL" in rr.json()["detail"]
    assert client2.get(f"/v1/candidates/{cid}").json()["stage"] == "SHADOW"
    server.should_exit = True


def test_auditor_pass_allows_promotion(tmp_path, monkeypatch):
    """Auditor 通过 → 晋级成功，结论 ID 进审计历史。"""
    import subprocess, sys, time, httpx, os
    db = tmp_path / "auditor.db"
    env = dict(os.environ, RISK_AUDITOR_DB=str(db))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "risk_auditor.main:app",
         "--port", "8096"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                httpx.get("http://127.0.0.1:8096/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        monkeypatch.setenv("STRATEGY_EVOLUTION_AUDITOR_URL",
                           "http://127.0.0.1:8096")
        import importlib
        client2 = client
        cid = client2.post("/v1/candidates", json={
            "market": "CRYPTO", "strategy_id": "momentum",
            "strategy_version": "1.0.0"}).json()["candidate_id"]
        for stage, n in [("BACKTESTED", 1), ("VALIDATED", 2),
                         ("PAPER", 2), ("SHADOW", 2)]:
            assert client2.post(f"/v1/candidates/{cid}/promote", json={
                "target_stage": stage, "evidence_refs": _evidence(n)}
                ).status_code == 200
        rr = client2.post(f"/v1/candidates/{cid}/promote", json={
            "target_stage": "APPROVED", "evidence_refs": _evidence(3),
            "approval_id": "appr-1"})
        assert rr.status_code == 200, rr.text
        assert rr.json()["stage"] == "APPROVED"
        history = client2.get(f"/v1/candidates/{cid}/audit-history").json()
        applied = [h for h in history if h["action"] == "promotion.applied"
                   and h["to_stage"] == "APPROVED"]
        assert applied and "auditor=audit-" in (applied[0]["detail"] or "")
    finally:
        proc.terminate()
