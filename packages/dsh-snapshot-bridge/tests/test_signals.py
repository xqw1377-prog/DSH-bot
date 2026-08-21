from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dsh_snapshot_bridge.ashare_signals import map_policy_decisions
from dsh_snapshot_bridge.crypto_signals import map_crypto_rejects, map_crypto_signals
from dsh_snapshot_bridge.export import export_ashare_snapshot, export_crypto_snapshot
from dsh_snapshot_bridge.schema import validate_snapshot


def test_crypto_maps_pending_without_client_order_id():
    rows = [
        {
            "client_order_id": "",
            "symbol": "HYPEUSDT",
            "side": "SHORT",
            "action": "pending",
            "signal_type": "V5",
            "signal_tier": "S",
            "timestamp": 1787129153.494387,
            "entry_price": "58.479",
            "reject_reason": "Waiting for 1 confirm bars",
        },
        {
            "symbol": "BNBUSDT",
            "side": "",
            "action": "filtered",
            "signal_type": "V5",
            "reject_reason": "No_Edge_Trigger",
            "timestamp": 1787129154,
            "entry_price": "597.4",
        },
    ]
    mapped = map_crypto_signals(rows, strategy_version="v5", snapshot_id="snap")
    assert mapped[0]["symbol"] == "HYPEUSDT"
    assert mapped[0]["side"] == "SELL"
    assert "Waiting for 1 confirm bars" in mapped[0]["why_source"]
    rejects = map_crypto_rejects(rows)
    assert rejects[0]["kind"] == "REJECTED_CANDIDATE"
    assert rejects[0]["symbol"] == "BNBUSDT"


def test_crypto_maps_pending_long_not_filtered():
    rows = [
        {
            "client_order_id": "cid-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "action": "pending",
            "signal_type": "V5",
            "signal_tier": "S",
            "timestamp": "2026-08-19T01:00:00+00:00",
            "entry_price": "67000",
        },
        {
            "client_order_id": "cid-skip",
            "symbol": "ETHUSDT",
            "side": "LONG",
            "action": "filtered",
            "signal_type": "V5",
            "timestamp": "2026-08-19T01:01:00+00:00",
        },
        {
            "client_order_id": "cid-sync",
            "symbol": "SOLUSDT",
            "side": "LONG",
            "action": "pending",
            "signal_type": "V5_SYNC_ADOPT",
            "timestamp": "2026-08-19T01:02:00+00:00",
        },
    ]
    mapped = map_crypto_signals(rows, strategy_version="v5-test", snapshot_id="snap")
    assert len(mapped) == 1
    assert mapped[0]["signal_id"] == "cid-1"
    assert mapped[0]["side"] == "BUY"
    assert mapped[0]["strength"] == 0.9
    assert mapped[0]["evidence_refs"]


def test_ashare_maps_executable_policy_not_screen():
    cockpit = {
        "policy_decisions": {
            "_decision_event_id": "evt-9",
            "contract_version": "zisu-1",
            "quotes_updated_at": "2026-08-18T02:00:00+00:00",
            "decisions": [
                {
                    "symbol": "600519.SH",
                    "action": "buy",
                    "executable": True,
                    "engine_id": "leaf",
                    "thesis_id": "t1",
                    "evidence_score": 0.81,
                    "reasons": ["趋势延续"],
                    "leaf_signal": {"confidence": 0.81},
                },
                {
                    "symbol": "000001.SZ",
                    "action": "buy",
                    "executable": False,
                    "engine_id": "leaf",
                },
                {
                    "symbol": "601318.SH",
                    "action": "hold",
                    "executable": True,
                    "engine_id": "leaf",
                },
            ],
        }
    }
    mapped = map_policy_decisions(cockpit, snapshot_id="snap")
    assert len(mapped) == 1
    assert mapped[0]["signal_id"] == "evt-9:600519:buy"
    assert mapped[0]["side"] == "BUY"
    assert mapped[0]["strategy_version"] == "zisu-1"


def test_export_crypto_reads_sidecar_jsonl(tmp_path, monkeypatch):
    out = tmp_path / "snap"
    src = tmp_path / "src"
    src.mkdir()
    state = {
        "last_update": datetime.now(UTC).timestamp(),
        "runtime_version": "v5-local",
        "balance": {
            "total_balance": "1000",
            "available_balance": "400",
            "margin_used": "600",
            "sync_ok": True,
        },
        "exchange_positions": {},
    }
    (src / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (src / "signals.jsonl").write_text(
        json.dumps(
            {
                "client_order_id": "cid-live",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "action": "pending",
                "signal_type": "V5",
                "signal_tier": "A",
                "timestamp": datetime.now(UTC).isoformat(),
                "entry_price": "67000",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (src / "trades.jsonl").write_text(
        json.dumps(
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "open_fill_price": 2400.1,
                "close_fill_price": 2300.2,
                "close_fill_qty": 0.5,
                "fee_usd": 0.4,
                "pnl_usd": 49.5,
                "exit_reason_cn": "移动止盈",
                "opened_at": datetime.now(UTC).timestamp(),
                "closed_at": datetime.now(UTC).timestamp(),
                "position_lifecycle_id": "life-export",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(out))
    snap = export_crypto_snapshot(state_path=src / "state.json", output_dir=out)
    validate_snapshot(snap)
    assert snap["signals"][0]["signal_id"] == "cid-live"
    assert snap["closed_trades"][0]["source_trade_id"] == "life-export"
    assert snap["fills"]


def test_export_ashare_cockpit_failure_keeps_wallet(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    wallet = {
        "updated_at": "2026-08-18T02:00:00+00:00",
        "cash_balance": "10",
        "total_asset": "10",
        "quote_health": {"ok": True},
        "positions": [],
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/wallet"):
                data = json.dumps(wallet).encode()
                self.send_response(200)
            elif self.path.endswith("/screen"):
                data = json.dumps({"actionable": []}).encode()
                self.send_response(200)
            else:
                data = b"no"
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("DSH_A_SHARE_WALLET_URL", f"http://127.0.0.1:{port}/api/paper/wallet")
    monkeypatch.setenv("DSH_A_SHARE_SCREEN_URL", f"http://127.0.0.1:{port}/api/trade/screen")
    snap = export_ashare_snapshot(output_dir=tmp_path / "out")
    server.shutdown()
    assert snap["accounts"]
    assert snap["signals"] == []
    assert snap["data_fresh"] is True
