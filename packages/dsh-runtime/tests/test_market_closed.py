from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dsh_contracts import Market
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_runtime.execution import TradeExecutionCore, a_share_session_open

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"
# 2026-08-18 周二：UTC 02:00 = 上海 10:00 开市；UTC 04:00 = 上海 12:00 午休。
OPEN_AT = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)
LUNCH_AT = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)


def test_a_share_session_closed_is_not_open():
    closed = datetime(2026, 8, 18, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    opened = datetime(2026, 8, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    lunch = datetime(2026, 8, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert a_share_session_open(closed) is False
    assert a_share_session_open(opened) is True
    assert a_share_session_open(lunch) is False
    assert a_share_session_open(OPEN_AT) is True
    assert a_share_session_open(LUNCH_AT) is False


def test_ashare_closed_does_not_open_incident():
    reset()

    class Gateway:
        def get_health(self, market):
            return {
                "system_ok": True,
                "data_fresh": False,
                "source_observed_at": LUNCH_AT.isoformat(),
            }

        def get_signals(self, market):
            raise AssertionError("closed session must not query signals")

        def request_approval(self, *args, **kwargs):
            raise AssertionError("closed session must not request approval")

    profile = load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    session = BotSession.for_profile(profile)
    agent = TradeExecutionCore(
        name="a-stock-bot",
        market=Market.A_SHARE,
        gateway=Gateway(),
        approvals=object(),
        account_id="paper-a-share-001",
        mode="shadow",
        now_fn=lambda: LUNCH_AT,
    )
    run_once(session, agent)
    assert session.memory.recent(kind="incident") == []
    assert session.events.query("incident/opened") == []
    assert session.tasks.find_by_status(
        "AWAITING_APPROVAL", "SHADOW_RECORDED", "DONE", "SIGNAL_RECEIVED"
    ) == []
    closed = session.memory.recent(kind="market-closed")
    assert closed and "闭市" in closed[0]["content"]
    reset()
