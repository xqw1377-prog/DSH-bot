import os

from fastapi.testclient import TestClient


def test_snapshot_read_key_cannot_write(tmp_path, monkeypatch):
    snap = tmp_path / "CRYPTO.json"
    snap.write_text(
        '{"health":{"system_ok":true,"data_fresh":true,"trading_channel_ok":false},'
        '"accounts":[{"account_id":"paper-crypto-001","cash":"1","equity":"1","currency":"USDT"}],'
        '"positions":[],"signals":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_ENV", "production")
    monkeypatch.setenv("DSH_LOCAL_PAPER", "0")
    monkeypatch.setenv("QUANT_GATEWAY_READ_ONLY", "1")
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("QUANT_GATEWAY_API_KEYS", "shadow-read/shadow-reader:read")
    monkeypatch.delenv("QUANT_CRYPTO_READONLY_URL", raising=False)

    from quant_gateway.adapters.registry import _adapters

    _adapters.clear()
    from quant_gateway.main import app

    with TestClient(app) as client:
        headers = {"X-API-Key": "shadow-read"}
        health = client.get("/v1/markets/CRYPTO/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["trading_channel_ok"] is False
        stop = client.post("/v1/markets/CRYPTO/emergency-stop", headers=headers)
        assert stop.status_code == 403
        order = client.post("/v1/markets/CRYPTO/orders", headers=headers, json={})
        assert order.status_code == 403
    _adapters.clear()


def test_write_scope_still_forbidden_when_read_only(tmp_path, monkeypatch):
    snap = tmp_path / "CRYPTO.json"
    snap.write_text(
        '{"health":{"system_ok":true,"data_fresh":true,"trading_channel_ok":false},'
        '"accounts":[{"account_id":"paper-crypto-001","cash":"1","equity":"1","currency":"USDT"}],'
        '"positions":[],"signals":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_ENV", "production")
    monkeypatch.setenv("DSH_LOCAL_PAPER", "0")
    monkeypatch.setenv("QUANT_GATEWAY_READ_ONLY", "1")
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv(
        "QUANT_GATEWAY_API_KEYS",
        "shadow-write/shadow-writer:read,write",
    )
    monkeypatch.delenv("QUANT_CRYPTO_READONLY_URL", raising=False)

    from quant_gateway.adapters.registry import _adapters

    _adapters.clear()
    from quant_gateway.main import app

    writes = (
        ("/v1/markets/CRYPTO/orders", {}),
        ("/v1/markets/CRYPTO/orders/x/cancel", {}),
        ("/v1/approvals", {"market": "CRYPTO"}),
        ("/v1/approvals/a1/decide", {"decision": "APPROVED"}),
        ("/v1/markets/CRYPTO/emergency-stop", {}),
        ("/v1/markets/CRYPTO/kill-switch/resume", {}),
        ("/v1/markets/CRYPTO/strategies/s1/pause", {}),
        ("/v1/markets/CRYPTO/strategies/s1/resume", {}),
        ("/v1/markets/CRYPTO/risk-snapshots", {"account_id": "paper-crypto-001"}),
    )
    with TestClient(app) as client:
        headers = {"X-API-Key": "shadow-write"}
        assert client.get("/v1/markets/CRYPTO/accounts", headers=headers).status_code == 200
        for path, body in writes:
            resp = client.post(path, headers=headers, json=body)
            assert resp.status_code == 403, (path, resp.status_code, resp.text)
    _adapters.clear()
