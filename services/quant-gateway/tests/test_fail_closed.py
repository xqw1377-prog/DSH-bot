"""失败关闭红线（NFR-002）。

未配置适配器的市场不得返回猜测数据，也不得放行任何资金动作。
"""

import pytest
from fastapi.testclient import TestClient

from quant_gateway.adapters.registry import _adapters
from quant_gateway.main import app


@pytest.fixture()
def client_without_adapters():
    _adapters.clear()
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    ["health", "positions", "accounts", "signals"],
)
def test_read_only_fails_closed_without_adapter(client_without_adapters, path):
    resp = client_without_adapters.get(f"/v1/markets/A_SHARE/{path}")
    assert resp.status_code == 503


def test_emergency_stop_fails_closed_without_adapter(client_without_adapters):
    resp = client_without_adapters.post("/v1/markets/CRYPTO/emergency-stop")
    assert resp.status_code == 503
