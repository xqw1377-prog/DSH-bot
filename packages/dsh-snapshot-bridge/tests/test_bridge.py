from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dsh_snapshot_bridge.ashare import a_share_session, map_ashare_payloads
from dsh_snapshot_bridge.atomic import atomic_write_json
from dsh_snapshot_bridge.crypto import map_crypto_state
from dsh_snapshot_bridge.export import export_ashare_snapshot, export_crypto_snapshot
from dsh_snapshot_bridge.schema import validate_snapshot
from dsh_snapshot_bridge.secrets import assert_no_secrets
from dsh_snapshot_bridge.symbols import (
    assert_no_symbol_collisions,
    normalize_ashare_symbol,
    restore_source_symbol,
)


def _crypto_state(**over):
    body = {
        "last_update": datetime.now(UTC).timestamp(),
        "balance": {
            "total_balance": "2276.15",
            "available_balance": "373.34",
            "margin_used": "1914.86",
            "sync_ok": True,
        },
        "exchange_positions": {
            "BNBUSDT": {
                "symbol": "BNBUSDT",
                "side": "BUY",
                "quantity": "2.17",
                "entry_price": "604.33",
            },
            "ETHUSDT": {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "quantity": "0.5",
                "entry_price": "2400.10",
            },
        },
    }
    body.update(over)
    return body


def test_crypto_maps_decimal_strings_and_signed_qty():
    snap = map_crypto_state(
        _crypto_state(),
        account_id="paper-crypto-001",
        source_system="6celue_v5",
        source_mode="demo",
        stale_after_seconds=90,
    )
    validate_snapshot(snap)
    assert snap["account_id"] == "paper-crypto-001"
    assert snap["signals"] == []
    buy = next(p for p in snap["positions"] if p["symbol"] == "BNBUSDT")
    sell = next(p for p in snap["positions"] if p["symbol"] == "ETHUSDT")
    assert buy["quantity"] == "2.17"
    assert sell["quantity"] == "-0.5"
    assert buy["avg_cost"] == "604.33"
    assert snap["health"]["trading_channel_ok"] is False


def test_crypto_stale_source_is_fail_closed(monkeypatch):
    old = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    snap = map_crypto_state(
        _crypto_state(last_update=old),
        account_id="paper-crypto-001",
        source_system="6celue_v5",
        source_mode="demo",
        stale_after_seconds=90,
    )
    assert snap["data_fresh"] is False
    assert snap["degraded"] is True
    assert "SOURCE_STALE" in snap["detail"]


def test_ashare_symbol_roundtrip_and_collision():
    symbol, source = normalize_ashare_symbol("600519.SH")
    assert symbol == "600519"
    assert restore_source_symbol(symbol, source) == "600519.SH"
    with pytest.raises(ValueError, match="collision"):
        assert_no_symbol_collisions(
            [
                {"symbol": "600519", "source_symbol": "600519.SH"},
                {"symbol": "600519", "source_symbol": "600519.SZ"},
            ]
        )


def test_ashare_screen_is_not_signal():
    wallet = {
        "updated_at": "2026-08-18T02:00:00+00:00",
        "cash_balance": "1048340",
        "total_asset": "1250000",
        "quote_health": {"ok": True},
        "positions": [
            {
                "symbol": "600519.SH",
                "quantity": 120,
                "sellable_quantity": 100,
                "t_plus_one_locked": 20,
                "avg_cost": "1680.50",
            }
        ],
        "recent_trades": [
            {"id": 9, "symbol": "600519.SH", "side": "buy", "quantity": 100, "price": "1680.5"}
        ],
    }
    screen = {
        "actionable": [
            {"symbol": "600519.SH", "policy_action": "buy", "executable": True, "engine_id": "leaf"}
        ]
    }
    snap = map_ashare_payloads(
        wallet,
        screen,
        account_id="paper-a-share-001",
        source_system="zisu",
        source_mode="paper",
        now=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
    )
    validate_snapshot(snap)
    assert snap["signals"] == []
    assert snap["screen_results"][0]["kind"] == "SCREEN_RESULT"
    assert snap["positions"][0]["source_symbol"] == "600519.SH"
    assert snap["positions"][0]["available_quantity"] == "100"
    assert snap["health"]["market_session"] == "CLOSED"


def test_market_closed_not_stale_when_api_ok():
    assert a_share_session(datetime(2026, 8, 18, 20, 0, tzinfo=UTC)) == "CLOSED"
    wallet = {
        "updated_at": "2026-08-18T12:00:00+00:00",
        "cash_balance": "1",
        "total_asset": "1",
        "quote_health": {"ok": False},
        "positions": [],
    }
    snap = map_ashare_payloads(
        wallet,
        {},
        account_id="paper-a-share-001",
        source_system="zisu",
        source_mode="paper",
        now=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
    )
    assert snap["data_fresh"] is True
    assert "market_closed" in snap["detail"]


def test_atomic_replace_and_invalid_json_keeps_last_books(tmp_path, monkeypatch):
    out = tmp_path / "snapshots"
    state = tmp_path / "state.json"
    state.write_text(json.dumps(_crypto_state()), encoding="utf-8")
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(out))
    first = export_crypto_snapshot(state_path=state, output_dir=out)
    assert first["data_fresh"] is True
    cash = first["accounts"][0]["cash"]
    state.write_text("{not-json", encoding="utf-8")
    second = export_crypto_snapshot(state_path=state, output_dir=out)
    assert second["data_fresh"] is False
    assert second["degraded"] is True
    assert second["accounts"][0]["cash"] == cash
    assert second["source_observed_at"] == first["source_observed_at"]
    assert second["exported_at"] != first["exported_at"]
    assert second["degraded"] is True
    assert "CRYPTO_STATE_INVALID_JSON" in second["detail"] or "JSON" in second["detail"]


def test_ashare_timeout_does_not_invent_books(tmp_path, monkeypatch):
    out = tmp_path / "snap"
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(out))
    monkeypatch.setenv("DSH_A_SHARE_WALLET_URL", "http://127.0.0.1:1/api/paper/wallet")
    monkeypatch.setenv("DSH_A_SHARE_SCREEN_URL", "http://127.0.0.1:1/api/trade/screen")
    monkeypatch.setenv("DSH_SNAPSHOT_HTTP_TIMEOUT_SEC", "0.2")
    snap = export_ashare_snapshot(output_dir=out, timeout=0.2)
    assert snap["accounts"] == []
    assert snap["positions"] == []
    assert snap["degraded"] is True
    assert snap["last_error_type"] in {"SOURCE_TIMEOUT", "SOURCE_UNAVAILABLE"}


def test_secrets_rejected():
    with pytest.raises(ValueError, match="secret"):
        assert_no_secrets({"accounts": [{"api_key": "abc"}]})
    with pytest.raises(ValueError, match="secret"):
        validate_snapshot(
            {
                "schema_version": "dsh-snapshot-1",
                "snapshot_id": "x",
                "market": "CRYPTO",
                "account_id": "paper-crypto-001",
                "source_system": "6celue_v5",
                "source_mode": "demo",
                "source_observed_at": "2026-08-18T00:00:00+00:00",
                "exported_at": "2026-08-18T00:00:00+00:00",
                "data_fresh": True,
                "degraded": False,
                "detail": "ok",
                "health": {
                    "system_ok": True,
                    "data_fresh": True,
                    "trading_channel_ok": False,
                    "degraded": False,
                    "detail": "postgres://user:pass@h/db",
                },
                "accounts": [],
                "positions": [],
                "orders": [],
                "signals": [],
            }
        )


def test_atomic_write_is_complete_file(tmp_path):
    path = tmp_path / "CRYPTO.json"
    atomic_write_json(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
