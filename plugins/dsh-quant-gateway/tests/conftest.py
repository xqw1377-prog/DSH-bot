"""用 httpx.MockTransport 模拟网关,不启动真实服务。"""

import httpx
import pytest

from dsh_gateway_client import GatewayClient

_real_client = httpx.Client


@pytest.fixture()
def mock_gateway(monkeypatch):
    """把 GatewayClient 内部创建的 httpx.Client 换成带 MockTransport 的实例。

    由各用例通过 mock_gateway.handler["handler"] 提供响应函数。
    """
    holder = {"handler": None}

    def fake_client_factory(*args, **kwargs):
        def delegate(request: httpx.Request) -> httpx.Response:
            return holder["handler"](request)

        kwargs["transport"] = httpx.MockTransport(delegate)
        return _real_client(*args, **kwargs)

    monkeypatch.setattr("dsh_gateway_client.client.httpx.Client", fake_client_factory)

    class _Gateway:
        url = "http://gateway.test"

    _Gateway.handler = holder
    yield _Gateway


@pytest.fixture()
def client(mock_gateway):
    return GatewayClient(base_url=mock_gateway.url)
