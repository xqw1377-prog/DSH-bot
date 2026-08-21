from dsh_snapshot_bridge.crypto_trades import (
    map_crypto_closed_trades,
    map_crypto_equity_curve,
    map_crypto_fills,
)


def test_maps_closed_trade_fees_and_exit_reason():
    closed = map_crypto_closed_trades(
        [
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "open_fill_price": 2400.1,
                "close_fill_price": 2300.2,
                "open_fill_qty": 0.5,
                "close_fill_qty": 0.5,
                "fee_usd": 0.41,
                "pnl_usd": 49.5,
                "pnl_r": 0.8,
                "exit_reason_cn": "移动止盈",
                "opened_at": 1787000000,
                "closed_at": 1787003600,
                "position_lifecycle_id": "life-1",
                "signal_id": "sig-1",
            }
        ],
        account_id="paper-crypto-001",
    )
    assert len(closed) == 1
    assert closed[0]["symbol"] == "ETHUSDT"
    assert closed[0]["side"] == "SELL"
    assert closed[0]["fee"] == "0.41"
    assert closed[0]["pnl"] == "49.5"
    assert closed[0]["exit_reason"] == "移动止盈"
    assert closed[0]["source_kind"] == "6celue_closed_trade"
    assert closed[0]["signal_id"] == "sig-1"
    fills = map_crypto_fills(closed)
    assert len(fills) == 2
    assert fills[0]["source_kind"] == "6celue_open_fill"
    assert fills[1]["fee"] == "0.41"
    assert fills[1]["source_kind"] == "6celue_close_fill"


def test_skips_incomplete_trade_and_maps_equity():
    assert map_crypto_closed_trades([{"symbol": "BTCUSDT", "side": "BUY"}], account_id="a") == []
    curve = map_crypto_equity_curve(
        [{"ts": 1787000000, "equity": 2000.5, "wallet": 1980, "upnl": 20.5}]
    )
    assert curve[0]["equity"] == "2000.5"
    assert curve[0]["ts"]
