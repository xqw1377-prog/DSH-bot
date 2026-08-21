from dsh_runtime.pipeline import BLOCKED_STAGES, run_optimization_pipeline


def _book():
    trades = []
    for index in range(10):
        lose = index % 2 == 0
        day = f"{index + 1:02d}"
        trades.append(
            {
                "pnl": "-30" if lose else "10",
                "pnl_r": "-3" if lose else "1",
                "fee": "0.2" if lose else "0.1",
                "closed_at": f"2026-08-{day}T00:00:00+00:00",
                "opened_at": f"2026-08-{day}T00:00:00+00:00",
            }
        )
    return trades


def test_walk_forward_promotes_to_shadow_and_blocks_trade():
    result = run_optimization_pipeline(_book(), market="CRYPTO")
    stop = next(item for item in result["candidates"] if item["suggestion_id"] == "replay-stop-1r")
    assert stop["stage"] == "SHADOW"
    assert stop["next_stage"] == "PAPER"
    assert stop["can_apply"] is False
    assert stop["trade_blocked"] is True
    assert stop["mae_mfe"] is False
    assert result["shadowed"]
    assert all(item["can_apply"] is False for item in result["candidates"])
    assert all(item["stage"] not in BLOCKED_STAGES for item in result["candidates"])


def test_small_sample_stays_before_shadow():
    trades = [
        {"pnl": "-30", "pnl_r": "-3", "closed_at": "2026-08-01T00:00:00+00:00"},
        {"pnl": "-20", "pnl_r": "-2", "closed_at": "2026-08-02T00:00:00+00:00"},
        {"pnl": "10", "pnl_r": "1", "closed_at": "2026-08-03T00:00:00+00:00"},
    ]
    result = run_optimization_pipeline(trades, market="CRYPTO")
    stop = next(item for item in result["candidates"] if item["suggestion_id"] == "replay-stop-1r")
    assert stop["stage"] == "REPLAY"
    assert stop["next_stage"] == "BACKTEST"
    assert stop["can_apply"] is False
