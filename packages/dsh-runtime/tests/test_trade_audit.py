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


def test_closed_trade_opens_and_closes_episode_with_chain():
    """第一刀链路：成交 → 决策 → 回合（含退出事实）→ 审计 → 优化候选。"""
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    result = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-ep-1",
                "signal_id": "sig-ep-1",
                "order_id": "ord-ep-1",
                "entry_price": "2000",
                "exit_price": "1900",
                "quantity": "1",
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
    row = session.ledger.list()[0]
    # 决策行暴露回合链路 ID
    assert row["episode_id"]
    episode = session.ledger.get_episode(row["episode_id"])
    assert episode["status"] == "CLOSED"
    assert episode["entry_fill_id"] == "life-ep-1"
    assert episode["entry_price"] == "2000"
    assert episode["exit_at"] == "2026-08-19T03:00:00+00:00"
    assert episode["exit_reason"] == "移动止盈"
    assert episode["realized_pnl"] == "40"
    # 回合幂等：重跑不另开回合
    again = ingest_and_audit_trades(
        session,
        market="CRYPTO",
        trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_trade_id": "life-ep-1",
                "signal_id": "sig-ep-1",
                "pnl": "40",
                "pnl_r": "0.8",
                "fee": "0.4",
            }
        ],
    )
    assert again["linked"] == 1
    assert len(session.ledger.episodes_for_decision(row["decision_id"])) == 1
    # 1h/1d/3d 结果回填只接受事实观察值
    tracked = session.ledger.record_episode_outcome(episode["episode_id"], "1d", "+1.2% vs 入场价")
    assert tracked["outcomes"]["1d"] == "+1.2% vs 入场价"
    try:
        session.ledger.record_episode_outcome(episode["episode_id"], "5m", "nope")
        raise AssertionError("invalid outcome key accepted")
    except ValueError:
        pass
    coverage = session.ledger.coverage()
    assert coverage["episodes_closed"] == 1
    assert coverage["episodes_open"] == 0
    # 优化候选入账本：带反事实数据，重跑幂等，can_apply 恒 False
    assert result["candidate_ids"] or not result["candidates"]
    for candidate_id in result["candidate_ids"]:
        saved = session.ledger.get_candidate(candidate_id)
        assert saved["can_apply"] is False
        assert saved["actual_pnl"] or saved["actual_pnl"] == "0"
    before = set(result["candidate_ids"])
    rerun = ingest_and_audit_trades(
        session, market="CRYPTO",
        trades=[{
            "symbol": "ETHUSDT", "side": "SELL", "source_trade_id": "life-ep-1",
            "signal_id": "sig-ep-1", "pnl": "40", "pnl_r": "0.8", "fee": "0.4",
        }],
    )
    assert set(rerun["candidate_ids"]) == before
    reset()


def test_optimization_candidates_persist_with_counterfactual():
    """优化候选必须带反事实数据入账本，重跑不重复，不可直接应用。"""
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    trades = []
    for index in range(6):
        pnl = "-30" if index % 2 else "10"
        trades.append(
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "source_trade_id": f"life-c-{index}",
                "signal_id": f"sig-c-{index}",
                "pnl": pnl,
                "pnl_r": "-3" if pnl == "-30" else "1",
                "fee": "0.4",
                "opened_at": f"2026-08-1{index}T00:00:00+00:00",
                "closed_at": f"2026-08-1{index}T06:00:00+00:00",
            }
        )
    result = ingest_and_audit_trades(session, market="CRYPTO", trades=trades)
    assert result["candidate_ids"], "足量样本下必须产出优化候选"
    for candidate_id in result["candidate_ids"]:
        saved = session.ledger.get_candidate(candidate_id)
        assert saved is not None
        assert saved["can_apply"] is False
        # 反事实数据不是凭空建议：重放实际/回放值必须在场
        assert saved["actual_pnl"] is not None
        assert saved["replayed_pnl"] is not None
        assert saved["evidence_refs"]
    rerun = ingest_and_audit_trades(session, market="CRYPTO", trades=trades)
    assert set(rerun["candidate_ids"]) == set(result["candidate_ids"])
    assert len(session.ledger.list_candidates(market="CRYPTO")) == len(result["candidate_ids"])
    reset()
