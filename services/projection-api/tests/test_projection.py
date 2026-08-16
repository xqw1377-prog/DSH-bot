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


def test_experiments_proxied_to_evolution(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_upstream["handler"] = handler
    client.get("/v1/experiments")
    assert ":8002" in seen["url"]
