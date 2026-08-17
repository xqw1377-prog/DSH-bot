"""projection-api 代理测试:用 MockTransport 模拟上游,验证路径与参数透传、
上游状态码原样传递(失败关闭不被压成 500)。"""

import httpx
import pytest
from fastapi.testclient import TestClient

import projection_api.main as projection_main
from projection_api.main import app

client = TestClient(app)


@pytest.fixture()
def mock_upstream(monkeypatch):
    holder = {"handler": None}
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        def delegate(request: httpx.Request) -> httpx.Response:
            return holder["handler"](request)

        kwargs["transport"] = httpx.MockTransport(delegate)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(projection_main.httpx, "AsyncClient", factory)
    return holder


def test_positions_proxied(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"symbol": "600519.SH"}])

    mock_upstream["handler"] = handler
    result = client.get("/v1/markets/A_SHARE/positions").json()
    assert result == [{"symbol": "600519.SH"}]
    assert seen["url"].endswith("/v1/markets/A_SHARE/positions")


def test_upstream_failure_status_preserved(mock_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "failing closed"})

    mock_upstream["handler"] = handler
    resp = client.get("/v1/markets/A_SHARE/positions")
    assert resp.status_code == 503


def test_unreachable_upstream_is_503(mock_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    mock_upstream["handler"] = handler
    assert client.get("/v1/markets/A_SHARE/positions").status_code == 503


def test_approvals_params_forwarded(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_upstream["handler"] = handler
    client.get("/v1/approvals", params={"status": "REQUESTED", "market": "A_SHARE"})
    assert "status=REQUESTED" in seen["url"]
    assert "market=A_SHARE" in seen["url"]


def test_chief_refuses_action_verbs(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_RUNTIME_DB", str(tmp_path / "missing.db"))
    resp = client.post("/v1/chief/query", json={"question": "请你立刻批准这笔订单"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is True
    assert "不能执行" in body["text"]


def test_chief_includes_health_context(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_RUNTIME_DB", str(tmp_path / "missing.db"))

    def fake_get(url, headers=None, timeout=2.0):
        market = "CRYPTO" if "CRYPTO" in url else "A_SHARE"
        return type(
            "R",
            (),
            {
                "is_success": True,
                "status_code": 200,
                "json": lambda self: {
                    "system_ok": True,
                    "data_fresh": market == "CRYPTO",
                },
            },
        )()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    resp = client.post("/v1/chief/query", json={"question": "现在系统健康吗"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is False
    assert "CRYPTO 正常" in body["text"]
    assert "A_SHARE 降级" in body["text"]
    assert "不含资金动作" in body["text"]


def test_incidents_empty_without_runtime_db(monkeypatch):
    monkeypatch.delenv("DSH_RUNTIME_DB", raising=False)

    def fake_get(url, params=None, headers=None, timeout=2.0):
        return type("R", (), {"is_success": True, "json": lambda self: []})()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    assert client.get("/v1/incidents").json() == []


def test_incidents_include_gateway_kill_switch_audit(monkeypatch):
    monkeypatch.delenv("DSH_RUNTIME_DB", raising=False)

    def fake_get(url, params=None, headers=None, timeout=2.0):
        assert "/v1/audit" in url
        return type(
            "R",
            (),
            {
                "is_success": True,
                "json": lambda self: [
                    {
                        "audit_id": "audit-ks-1",
                        "occurred_at": "2026-08-17T00:00:00+00:00",
                        "actor": "alice",
                        "action": "kill_switch.succeeded",
                        "market": "CRYPTO",
                        "subject_id": "paper-crypto-001",
                        "outcome": "OK",
                        "detail": "emergency_stop engaged",
                    }
                ],
            },
        )()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    rows = client.get("/v1/incidents").json()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "kill_switch/succeeded"
    assert rows[0]["source"] == "gateway-audit"


def test_experiments_proxied_to_evolution(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_upstream["handler"] = handler
    client.get("/v1/experiments")
    assert ":8002" in seen["url"]
