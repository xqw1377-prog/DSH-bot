from pathlib import Path

from dsh_runtime import BotSession, load_profile, reset
from dsh_runtime.snapshot_decisions import record_snapshot_decisions

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


def test_official_signal_is_shadow_only():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    first = record_snapshot_decisions(
        session,
        market="CRYPTO",
        signals=[
            {
                "signal_id": "sig-eth-pending",
                "symbol": "ETHUSDT",
                "side": "SELL",
                "source_action": "pending",
                "strategy_id": "6celue-v5",
                "strategy_version": "v5",
                "strength": 0.7,
                "entry_price": "4000",
                "quantity": "0.01",
                "why_source": ["等待确认"],
                "evidence_refs": ["6celue:signals.jsonl:sig-eth-pending"],
            }
        ],
    )
    assert first["recorded"] == 1
    row = session.ledger.find_by_signal("sig-eth-pending")
    assert row["status"] == "SHADOW"
    assert row["action"] == "WATCH"
    assert row["can_apply"] is False
    assert row["payload"]["trade_blocked"] is True
    assert row["entry_plan"]["trigger_price"] == "4000"
    assert row["exit_plan"]["stop_loss"] == "-1R"
    tasks = session.tasks.find_by_status("SHADOW_RECORDED")
    assert len(tasks) == 1
    second = record_snapshot_decisions(
        session,
        market="CRYPTO",
        signals=[{"signal_id": "sig-eth-pending", "symbol": "ETHUSDT", "side": "SELL"}],
    )
    assert second["recorded"] == 0
    assert second["skipped"] == 1
    reset()
