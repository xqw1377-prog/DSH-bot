import json
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException

from dsh_contracts import Market
from quant_gateway.adapters.snapshot import SnapshotAdapter, register_snapshot_adapters
from quant_gateway.adapters.registry import _adapters, get_adapter


def _write_snapshot(directory: Path) -> Path:
    path = directory / "CRYPTO.json"
    path.write_text(
        json.dumps(
            {
                "health": {"system_ok": True, "data_fresh": True, "trading_channel_ok": False},
                "positions": [
                    {
                        "account_id": "snap-1",
                        "symbol": "BTCUSDT",
                        "quantity": "0.2",
                        "available_quantity": "0.2",
                        "frozen_quantity": "0",
                        "avg_cost": "100",
                        "currency": "USDT",
                    }
                ],
                "accounts": [
                    {
                        "account_id": "snap-1",
                        "cash": "50",
                        "equity": "70",
                        "available_cash": "50",
                        "frozen_cash": "0",
                        "currency": "USDT",
                    }
                ],
                "signals": [
                    {
                        "signal_id": "s1",
                        "strategy_id": "s",
                        "strategy_version": "1",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "strength": 0.8,
                        "generated_at": "2026-08-17T00:00:00+00:00",
                        "valid_until": "2026-08-18T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_snapshot_reads_books_and_refuses_writes(tmp_path):
    path = _write_snapshot(tmp_path)
    adapter = SnapshotAdapter(Market.CRYPTO, path)
    assert adapter.get_health().system_ok is True
    assert adapter.get_positions()[0].quantity == adapter.get_positions()[0].available_quantity
    assert adapter.get_account_summary()[0].account_id == "snap-1"
    assert adapter.get_signals()[0].signal_id == "s1"
    try:
        adapter.request_order({"symbol": "BTCUSDT"})
        raise AssertionError("expected 403")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_register_snapshot_adapters(tmp_path, monkeypatch):
    _write_snapshot(tmp_path)
    _adapters.clear()
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path))
    register_snapshot_adapters()
    assert get_adapter(Market.CRYPTO).get_signals()[0].signal_id == "s1"
    _adapters.clear()


def test_public_ticker_overlays_price(tmp_path, monkeypatch):
    path = _write_snapshot(tmp_path)
    monkeypatch.setenv(
        "QUANT_GATEWAY_PUBLIC_TICKER_URL",
        "https://example.test/ticker?symbol={symbol}",
    )

    def fake_get(url, timeout=2.0):
        assert "BTCUSDT" in url
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"price": "67420"}})()

    monkeypatch.setattr("httpx.get", fake_get)
    adapter = SnapshotAdapter(Market.CRYPTO, path)
    pos = adapter.get_positions()[0]
    assert str(pos.avg_cost) == "67420"
    health = adapter.get_health()
    assert health.data_fresh is True
    assert "overlaid" in health.detail


def test_snapshot_metadata_and_skips_screen_results(tmp_path):
    path = tmp_path / "A_SHARE.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "dsh-snapshot-1",
                "snapshot_id": "snap-1",
                "market": "A_SHARE",
                "account_id": "paper-a-share-001",
                "source_system": "zisu",
                "source_mode": "paper",
                "source_observed_at": "2026-08-18T02:00:00+00:00",
                "exported_at": "2026-08-18T02:00:05+00:00",
                "data_fresh": True,
                "degraded": False,
                "detail": "source=zisu; session=CLOSED",
                "health": {
                    "system_ok": True,
                    "data_fresh": True,
                    "trading_channel_ok": False,
                    "degraded": False,
                    "detail": "source=zisu; session=CLOSED",
                },
                "positions": [],
                "accounts": [],
                "signals": [
                    {
                        "kind": "SCREEN_RESULT",
                        "signal_id": "should-skip",
                        "strategy_id": "leaf",
                        "symbol": "600519",
                        "side": "BUY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    adapter = SnapshotAdapter(Market.A_SHARE, path)
    health = adapter.get_health()
    assert health.source_system == "zisu"
    assert health.source_mode == "paper"
    assert health.trading_channel_ok is False
    assert health.as_of.isoformat().startswith("2026-08-18T02:00:05")
    assert adapter.get_signals() == []


def test_public_ticker_fail_closed_freshness(tmp_path, monkeypatch):
    path = _write_snapshot(tmp_path)
    monkeypatch.setenv(
        "QUANT_GATEWAY_PUBLIC_TICKER_URL",
        "https://example.test/ticker?symbol={symbol}",
    )

    def fake_get(url, timeout=2.0):
        raise TimeoutError("down")

    monkeypatch.setattr("httpx.get", fake_get)
    adapter = SnapshotAdapter(Market.CRYPTO, path)
    assert adapter.get_positions()[0].avg_cost == Decimal("100")
    health = adapter.get_health()
    assert health.data_fresh is False
    assert health.degraded is True
    assert "unreachable" in health.detail
