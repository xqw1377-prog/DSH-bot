from dsh_contracts import Market

from quant_gateway.adapters.http_readonly import HttpReadOnlyAdapter


def test_http_readonly_refuses_writes():
    adapter = HttpReadOnlyAdapter("http://example.invalid", Market.CRYPTO)
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        adapter.request_order({"symbol": "BTCUSDT"})
    assert exc.value.status_code == 403


def test_http_readonly_maps_upstream(monkeypatch):
    adapter = HttpReadOnlyAdapter("http://quant.local", Market.CRYPTO)

    def fake_get(self, path):
        if path == "/healthz":
            return {"status": "ok", "data_fresh": True, "trading_channel_ok": False}
        if path == "/accounts":
            return [{"account_id": "c-1", "cash": "10", "equity": "12", "currency": "USDT"}]
        if path == "/positions":
            return [{"account_id": "c-1", "symbol": "BTCUSDT", "quantity": "0.1",
                     "available_quantity": "0.1", "avg_cost": "100"}]
        if path == "/signals":
            return []
        raise AssertionError(path)

    monkeypatch.setattr(HttpReadOnlyAdapter, "_get", fake_get)
    health = adapter.get_health()
    assert health.system_ok is True
    assert health.trading_channel_ok is False
    assert adapter.get_account_summary()[0].account_id == "c-1"
    assert adapter.get_positions()[0].symbol == "BTCUSDT"
    assert adapter.get_signals() == []
