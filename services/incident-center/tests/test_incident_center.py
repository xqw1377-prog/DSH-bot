"""Incident Center 验收：指纹去重、生命周期、时间线 append-only、重启恢复。"""

import pytest
from fastapi.testclient import TestClient

from incident_center import main as m

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def clean():
    m.reset()
    yield
    m.reset()


def _open(source="crypto-bot", reason="order UNKNOWN beyond quarantine",
          subject="ord-1", market="CRYPTO"):
    return client.post("/v1/incidents", json={
        "source": source, "reason": reason, "subject": subject,
        "market": market, "severity": "HIGH",
    })


def test_fingerprint_dedupes_repeated_reports():
    first = _open().json()
    second = _open().json()
    third = _open().json()
    assert first["incident_id"] == second["incident_id"] == third["incident_id"]
    assert third["occurrences"] == 3
    assert len(client.get("/v1/incidents?status=OPEN").json()) == 1


def test_different_fingerprints_are_separate_incidents():
    _open(subject="ord-1")
    _open(subject="ord-2")
    assert len(client.get("/v1/incidents").json()) == 2


def test_lifecycle_and_illegal_transitions():
    inc = _open().json()
    iid = inc["incident_id"]
    # OPEN -> RESOLVED 非法（必须先缓解）
    r = client.post(f"/v1/incidents/{iid}/resolve",
                    json={"actor": "human"})
    assert r.status_code == 422
    # OPEN -> MITIGATED 合法
    r = client.post(f"/v1/incidents/{iid}/mitigate",
                    json={"actor": "human", "note": "手动平仓核查"})
    assert r.status_code == 200 and r.json()["status"] == "MITIGATED"
    # MITIGATED -> RESOLVED 合法
    r = client.post(f"/v1/incidents/{iid}/resolve",
                    json={"actor": "human", "note": "对账补齐"})
    assert r.json()["status"] == "RESOLVED"
    # RESOLVED 是终态
    r = client.post(f"/v1/incidents/{iid}/mitigate", json={"actor": "x"})
    assert r.status_code == 422


def test_resolved_incident_reopens_on_new_report():
    inc = _open().json()
    iid = inc["incident_id"]
    client.post(f"/v1/incidents/{iid}/mitigate", json={"actor": "h"})
    client.post(f"/v1/incidents/{iid}/resolve", json={"actor": "h"})
    again = _open().json()
    assert again["status"] == "OPEN"          # 重新打开
    assert again["occurrences"] == 2          # 计数累计
    tl = client.get(f"/v1/incidents/{iid}/timeline").json()
    actions = [t["action"] for t in tl]
    assert actions == ["opened", "mitigated", "resolved", "reopened"]


def test_timeline_is_append_only_complete():
    inc = _open().json()
    iid = inc["incident_id"]
    _open()  # re-report
    tl = client.get(f"/v1/incidents/{iid}/timeline").json()
    assert [t["action"] for t in tl] == ["opened", "re-reported"]
    assert all(t["actor"] for t in tl)


def test_restart_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_CENTER_DB", str(tmp_path / "inc.db"))
    m.reset()
    inc = _open().json()
    iid = inc["incident_id"]
    client.post(f"/v1/incidents/{iid}/mitigate", json={"actor": "h"})
    m.reset()  # 模拟重启
    after = client.get(f"/v1/incidents/{iid}").json()
    assert after["status"] == "MITIGATED"
    assert after["occurrences"] == 1
    tl = client.get(f"/v1/incidents/{iid}/timeline").json()
    assert [t["action"] for t in tl] == ["opened", "mitigated"]


def test_404_for_unknown_incident():
    assert client.get("/v1/incidents/inc-nope").status_code == 404
