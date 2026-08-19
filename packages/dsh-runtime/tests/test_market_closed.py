from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dsh_contracts import Market
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_runtime.execution import TradeExecutionCore, a_share_session_open

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


def test_a_share_session_closed_is_not_open():
    closed = datetime(2026, 8, 18, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    opened = datetime(2026, 8, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert a_share_session_open(closed) is False
    assert a_share_session_open(opened) is True


def test_ashare_closed_does_not_open_incident(monkeypatch):
    reset()
    monkeypatch.setattr(
        "dsh_runtime.execution.a_share_session_open", lambda now=None: False
    )

    class Gateway:
        def get_health(self, market):
            return {"system_ok": True, "data_fresh": False}

        def get_signals(self, market):
            raise AssertionError("closed session must not query signals")

    profile = load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    session = BotSession.for_profile(profile)
    agent = TradeExecutionCore(
        name="a-stock-bot",
        market=Market.A_SHARE,
        gateway=Gateway(),
        approvals=object(),
        account_id="paper-a-share-001",
        mode="shadow",
    )
    run_once(session, agent)
    assert session.memory.recent(kind="incident") == []
    closed = session.memory.recent(kind="market-closed")
    assert closed and "闭市" in closed[0]["content"]
    reset()
