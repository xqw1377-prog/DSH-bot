from datetime import UTC, datetime, timedelta

import httpx
import pytest
from dsh_contracts import Market, OrderIntent

from dsh_gateway_client import GatewayClient, GatewayError, new_idempotency_key


def make_intent(**overrides) -> OrderIntent:
    base = dict(
        idempotency_key=new_idempotency_key(),
        market=Market.A_SHARE,
        account_id="acc-1",
        strategy_id="strat-1",
        strategy_version="0.1.0",
        symbol="600519.SH",
        side="BUY",
        quantity="100",
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
        signal_snapshot_id="sig-1",
        risk_snapshot_id="risk-1",
    )
    base.update(overrides)
    return OrderIntent(**base)


def _with_handler(mock_gateway, handler, client):
    mock_gateway.handler["handler"] = handler
    return client


def test_request_order_success(mock_gateway, client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"order_id": "A_SHARE-ord-1", "status": "SUBMITTED"})

    _with_handler(mock_gateway, handler, client)
    result = client.request_order(make_intent())
    assert result["status"] == "SUBMITTED"
    assert seen["path"] == "/v1/markets/A_SHARE/orders"
    assert b"idempotency_key" in seen["body"]


def test_request_order_fail_closed_on_reject(mock_gateway, client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "approval required"})

    _with_handler(mock_gateway, handler, client)
    with pytest.raises(GatewayError) as exc_info:
        client.request_order(make_intent())
    assert exc_info.value.status_code == 422
    assert "approval required" in exc_info.value.detail


def test_list_approvals_passes_params(mock_gateway, client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    _with_handler(mock_gateway, handler, client)
    assert client.list_approvals(status="REQUESTED") == []
    assert "status=REQUESTED" in seen["url"]


def test_unreachable_gateway_raises(mock_gateway, client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _with_handler(mock_gateway, handler, client)
    with pytest.raises(httpx.ConnectError):
        client.request_order(make_intent())


def test_idempotency_key_unique_and_prefixed():
    keys = {new_idempotency_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(k.startswith("dsh-") for k in keys)
