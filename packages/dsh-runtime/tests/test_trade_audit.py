from pathlib import Path

from dsh_runtime import BotSession, load_profile, reset
from dsh_runtime.trade_audit import ingest_and_audit_trades, load_closed_trades, replay_exit_candidates

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


def test_import_trade_writes_fill_and_audit():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    result = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-1",
                "signal_id": "sig-eth-1",
                "order_id": "ord-1",
                "entry_price": "2000",
                "exit_price": "1900",
                "pnl": "40",
                "pnl_r": "0.8",
                "fee": "0.4",
                "exit_reason": "移动止盈",
                "signal_type": "V5",
                "opened_at": "2026-08-19T00:00:00+00:00",
                "closed_at": "2026-08-19T03:00:00+00:00",
            }
        ],
    )
    assert result["imported"] == 1
    assert result["audited"] == 1
    assert result["mae_mfe"] is False
    assert result["can_apply"] is False
    row = session.ledger.list()[0]
    assert row["fill_id"] == "life-1"
    assert row["signal_id"] == "sig-eth-1"
    assert row["order_id"] == "ord-1"
    assert row["audit_id"]
    assert row["payload"]["audit"]["dimensions"]["exit"]["note"]
    second = ingest_and_audit_trades(session, market="CRYPTO", trades=[
        {
            "symbol": "ETHUSDT",
            "side": "SELL",
            "source_trade_id": "life-1",
            "signal_id": "sig-eth-1",
            "pnl": "40",
            "pnl_r": "0.8",
            "fee": "0.4",
        }
    ])
    assert second["linked"] == 1
    assert second["imported"] == 0
    for index in range(30):
        session.ledger.upsert(
            {
                "market": "CRYPTO",
                "symbol": "BTCUSDT",
                "status": "SHADOW",
                "intel_grade": "OBSERVE",
                "execution_lane": "OBSERVE",
                "event_id": f"noise-{index}",
                "strategy_id": "noise",
                "strategy_version": "noise",
                "risk_snapshot_id": f"rs-noise-{index}",
                "requires_approval": True,
                "action": "WATCH",
                "payload": {"can_apply": False},
            }
        )
    third = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-1",
                "signal_id": "sig-eth-1",
                "pnl": "40",
                "pnl_r": "0.8",
                "fee": "0.4",
            }
        ],
    )
    assert third["linked"] == 1
    assert third["imported"] == 0
    reset()


def test_attach_fill_by_existing_signal_id():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    session.ledger.upsert(
        {
            "market": "CRYPTO",
            "symbol": "ETHUSDT",
            "status": "SHADOW",
            "intel_grade": "OFFICIAL_PREAUTH",
            "execution_lane": "SHADOW",
            "event_id": "evt-eth-sig",
            "signal_id": "sig-eth-1",
            "strategy_id": "intelligence-v1",
            "strategy_version": "intelligence-v1",
            "risk_snapshot_id": "rs-1",
            "requires_approval": True,
            "action": "SELL",
            "payload": {"can_apply": False},
        }
    )
    result = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-1",
                "signal_id": "sig-eth-1",
                "order_id": "ord-1",
                "pnl": "-30",
                "pnl_r": "-3",
                "fee": "0.4",
                "opened_at": "2026-08-17T00:00:00+00:00",
                "closed_at": "2026-08-20T01:00:00+00:00",
                "exit_reason": "时间止损",
            }
        ],
    )
    assert result["linked"] == 1
    assert result["imported"] == 0
    row = session.ledger.find_by_signal("sig-eth-1")
    assert row["fill_id"] == "life-1"
    assert row["status"] == "FILLED"
    assert row["audit_id"]
    assert row["payload"]["audit"]["mae_mfe"] is False
    assert "止损过晚" in row["payload"]["audit"]["dimensions"]["exit"]["note"]
    session.ledger.upsert(
        {
            "market": "CRYPTO",
            "symbol": "ETHUSDT",
            "status": "SHADOW",
            "intel_grade": "OFFICIAL_PREAUTH",
            "execution_lane": "SHADOW",
            "event_id": "evt-eth-sig",
            "signal_id": "sig-eth-1",
            "strategy_id": "intelligence-v1",
            "strategy_version": "intelligence-v1",
            "risk_snapshot_id": "rs-1",
            "requires_approval": True,
            "action": "SELL",
            "payload": {"can_apply": False},
        }
    )
    kept = session.ledger.find_by_signal("sig-eth-1")
    assert kept["fill_id"] == "life-1"
    assert kept["payload"]["trade"]["source_trade_id"] == "life-1"
    reset()


def test_second_trade_same_signal_is_imported():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    session.ledger.upsert(
        {
            "market": "CRYPTO",
            "symbol": "ETHUSDT",
            "status": "SHADOW",
            "intel_grade": "OFFICIAL_PREAUTH",
            "execution_lane": "SHADOW",
            "event_id": "evt-eth-sig",
            "signal_id": "sig-shared",
            "strategy_id": "intelligence-v1",
            "strategy_version": "intelligence-v1",
            "risk_snapshot_id": "rs-1",
            "requires_approval": True,
            "action": "SELL",
            "payload": {"can_apply": False},
        }
    )
    result = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-1",
                "signal_id": "sig-shared",
                "pnl": "10",
                "pnl_r": "1",
                "fee": "0.1",
            },
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-2",
                "signal_id": "sig-shared",
                "pnl": "-20",
                "pnl_r": "-2",
                "fee": "0.2",
            },
        ],
    )
    assert result["linked"] == 1
    assert result["imported"] == 1
    assert result["audited"] == 2
    fills = {row["fill_id"] for row in session.ledger.list() if row.get("fill_id")}
    assert fills == {"life-1", "life-2"}
    reset()


def test_load_closed_trades_reads_crypto_snapshot(tmp_path):
    (tmp_path / "CRYPTO.json").write_text(
        '{"closed_trades":[{"symbol":"ETHUSDT","source_trade_id":"life-1","pnl_r":"-2"}]}',
        encoding="utf-8",
    )
    rows = load_closed_trades(tmp_path, "CRYPTO")
    assert rows[0]["source_trade_id"] == "life-1"


def test_replay_stop_1r_stays_suggestion():
    trades = [
        {"pnl": "-30", "pnl_r": "-3"},
        {"pnl": "-20", "pnl_r": "-2"},
        {"pnl": "10", "pnl_r": "1"},
    ]
    items = replay_exit_candidates(trades)
    assert items[0]["suggestion_id"] == "replay-stop-1r"
    assert items[0]["can_apply"] is False
    assert items[0]["next_stage"] == "BACKTEST"
    assert "delta" in items[0]["evidence"][3]
