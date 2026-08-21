from projection_api.today import build_today


def test_today_explains_empty_official_signals():
    board = build_today(
        crypto_health={"market_session": "OPEN", "data_fresh": True},
        ashare_health={"market_session": "CLOSED", "data_fresh": True},
        crypto_account={"equity": "2394.52", "cash": "2084.15"},
        ashare_account={"equity": "1032707.16", "cash": "790872.86"},
        crypto_signals=[],
        ashare_signals=[],
        crypto_watch={
            "rejected_candidates": [
                {"symbol": "ENAUSDT", "reason": "No_Edge_Trigger"},
                {"symbol": "BNBUSDT", "reason": "Omega_Below_Threshold"},
            ]
        },
        ashare_watch={
            "screen_results": [
                {"kind": "SCREEN_RESULT", "symbol": "688498", "policy_action": "buy"}
            ]
        },
        decisions=[],
    )
    assert "没有正式可执行信号" in board["headline"]
    assert board["stories"][0]["market"] == "CRYPTO"
    assert any("挡掉" in point for point in board["stories"][0]["points"])
    assert any("选股" in point for point in board["stories"][1]["points"])
    assert board["disclaimer"].startswith("仅模拟")


def test_today_uses_current_waiting_signal_not_stale_buy():
    board = build_today(
        crypto_health={"market_session": "OPEN"},
        ashare_health={"market_session": "CLOSED", "data_fresh": True},
        crypto_account={"equity": "2395.18", "cash": "2084.15"},
        ashare_account={"equity": "1032707.16", "cash": "790872.86"},
        crypto_signals=[
            {
                "symbol": "HYPEUSDT",
                "side": "SELL",
                "strength": 0.9,
                "why_source": ["Waiting for 1 confirm bars"],
            },
            {
                "symbol": "SOLUSDT",
                "side": "SELL",
                "strength": 0.7,
                "why_source": ["Waiting for 1 confirm bars"],
            },
        ],
        ashare_signals=[
            {"symbol": "688498", "side": "BUY", "strength": 0.52},
            {"symbol": "300604", "side": "SELL", "strength": 0.43},
        ],
        crypto_watch={},
        ashare_watch={"screen_results": [{"kind": "SCREEN_RESULT", "symbol": "688498"}]},
        decisions=[
            {
                "task_id": "old",
                "market": "CRYPTO",
                "symbol": "BTCUSDT",
                "action": "BUY",
                "strength": 0.9,
                "updated_at": "2026-08-19T00:00:00+00:00",
            },
            {
                "task_id": "exp",
                "market": "CRYPTO",
                "symbol": "SOLUSDT",
                "action": "ABANDON",
                "skip_reason": "SIGNAL_EXPIRED",
                "updated_at": "2026-08-19T08:59:34+00:00",
            },
        ],
    )
    assert "HYPEUSDT" in board["headline"]
    assert "等待确认" in board["headline"]
    assert "BTCUSDT" not in board["headline"]
    assert board["stories"][0]["title"].startswith("Crypto 在等确认")
    assert "闭市" in board["stories"][1]["title"]
    assert board["counts"]["watch"] == 2
    assert board["counts"]["execute"] == 0


def test_today_leads_with_execute_decision():
    board = build_today(
        crypto_health={"market_session": "OPEN"},
        ashare_health={"market_session": "CLOSED"},
        crypto_account={"equity": "1", "cash": "1"},
        ashare_account={"equity": "1", "cash": "1"},
        crypto_signals=[{"symbol": "HYPEUSDT", "side": "SELL", "strength": 0.9}],
        ashare_signals=[],
        crypto_watch={},
        ashare_watch={},
        decisions=[
            {
                "task_id": "t1",
                "market": "CRYPTO",
                "symbol": "HYPEUSDT",
                "action": "SELL",
                "suggested_price": "58.4",
                "outcome_price": "58.1",
                "simulated_pnl": "0.003",
                "updated_at": "2026-08-19T09:00:00+00:00",
            }
        ],
    )
    assert "HYPEUSDT" in board["headline"]
    assert "卖出" in board["headline"]
    assert "建议卖出" in board["stories"][0]["title"]
