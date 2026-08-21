from datetime import UTC, datetime, timedelta
from pathlib import Path

from dsh_contracts import Market
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_runtime.execution import TradeExecutionCore

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"
NOW = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)


class _Gateway:
    def __init__(self, *, signals, health=None, positions=None, fail_preview=False):
        self.signals = signals
        self.health = health or {
            "system_ok": True,
            "data_fresh": True,
            "market_session": "OPEN",
        }
        self.positions = positions or [
            {"symbol": "BTCUSDT", "avg_cost": "68000", "available_quantity": "0.1"}
        ]
        self.fail_preview = fail_preview
        self.previewed = 0

    def get_health(self, market):
        return self.health

    def get_signals(self, market):
        return self.signals

    def get_positions(self, market, account_id=None):
        return self.positions

    def get_account_summary(self, market):
        return [{"account_id": "paper-crypto-001", "cash": "1000"}]

    def preview_order(self, intent):
        self.previewed += 1
        if self.fail_preview:
            exc = RuntimeError("preview down")
            exc.status_code = 503
            raise exc
        return {
            "estimated_cost": "670",
            "estimated_slippage": "0",
            "risk": {
                "risk_snapshot_id": "rs-1",
                "position_after": "0.01",
                "risk_budget_delta": "670",
                "worst_case_loss": "67",
                "limits_hit": [],
            },
        }


def _agent(gateway, mode="shadow", market=Market.CRYPTO, name="crypto-bot", **kwargs):
    profile = "crypto-bot" if market == Market.CRYPTO else "a-stock-bot"
    session = BotSession.for_profile(load_profile(PROFILES / profile / "profile.yaml"))
    agent = TradeExecutionCore(
        name=name,
        market=market,
        gateway=gateway,
        approvals=object(),
        account_id="crypto-paper-1" if market == Market.CRYPTO else "paper-a-share-001",
        mode=mode,
        now_fn=lambda: NOW,
        **kwargs,
    )
    return agent, session


def _signal(**over):
    body = {
        "signal_id": "sig-1",
        "market": "CRYPTO",
        "strategy_id": "6celue-v5",
        "strategy_version": "v5",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "strength": 0.9,
        "generated_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=15)).isoformat(),
        "data_snapshot_id": "snap",
        "evidence_refs": ["6celue:signals.jsonl:sig-1"],
        "quantity": "0.01",
        "entry_price": "67000",
    }
    body.update(over)
    return body


def test_shadow_records_once_and_tracks_outcome():
    reset()
    gateway = _Gateway(signals=[_signal()])
    agent, session = _agent(gateway)
    run_once(session, agent)
    run_once(session, agent)
    tasks = session.tasks.find_by_status("SHADOW_RECORDED")
    assert len(tasks) == 1
    decision = tasks[0]["payload"]["shadow_decision"]
    assert decision["action"] == "BUY"
    assert decision["disclaimer"] == "仅模拟，不会下单"
    assert decision["outcome_price"] == "68000"
    assert decision["simulated_pnl"] is not None
    reset()


def test_shadow_abandon_on_closed_and_stale():
    reset()
    gateway = _Gateway(
        signals=[_signal(signal_id="closed-1", market="A_SHARE", symbol="600519")],
        health={"system_ok": True, "data_fresh": False, "market_session": "CLOSED"},
    )
    agent, session = _agent(gateway, market=Market.A_SHARE, name="a-stock-bot")
    run_once(session, agent)
    task = session.tasks.find_by_status("SHADOW_RECORDED")[0]
    assert task["payload"]["shadow_decision"]["skip_reason"] == "MARKET_CLOSED"
    assert session.events.query("incident/opened") == []
    reset()

    gateway = _Gateway(
        signals=[_signal(signal_id="stale-1")],
        health={"system_ok": True, "data_fresh": False},
    )
    agent, session = _agent(gateway)
    run_once(session, agent)
    task = session.tasks.find_by_status("SHADOW_RECORDED")[0]
    assert task["payload"]["shadow_decision"]["skip_reason"] == "DATA_STALE"
    reset()


def test_shadow_low_strength_and_expired():
    reset()
    gateway = _Gateway(signals=[_signal(signal_id="weak", strength=0.1)])
    agent, session = _agent(gateway)
    run_once(session, agent)
    assert session.tasks.find_by_status("SHADOW_RECORDED")[0]["payload"]["shadow_decision"][
        "skip_reason"
    ] == "LOW_STRENGTH"
    reset()
    gateway = _Gateway(
        signals=[_signal(signal_id="old", valid_until=(NOW - timedelta(minutes=1)).isoformat())]
    )
    agent, session = _agent(gateway)
    run_once(session, agent)
    assert session.tasks.find_by_status("SHADOW_RECORDED")[0]["payload"]["shadow_decision"][
        "skip_reason"
    ] == "SIGNAL_EXPIRED"
    reset()


def test_shadow_waiting_confirm_is_watch_not_expired_sell():
    reset()
    gateway = _Gateway(
        signals=[
            _signal(
                signal_id="wait-1",
                side="SELL",
                symbol="HYPEUSDT",
                valid_until=(NOW - timedelta(minutes=20)).isoformat(),
                why_source=["Waiting for 1 confirm bars"],
            )
        ]
    )
    agent, session = _agent(gateway)
    run_once(session, agent)
    decision = session.tasks.find_by_status("SHADOW_RECORDED")[0]["payload"]["shadow_decision"]
    assert decision["action"] == "WATCH"
    assert decision["skip_reason"] == "WAITING_CONFIRM"
    assert decision["action"] != "SELL"
    assert decision["skip_reason"] != "SIGNAL_EXPIRED"
    reset()
