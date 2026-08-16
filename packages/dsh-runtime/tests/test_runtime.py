"""DSH Runtime 测试：Profile 能力检查、记忆/事件持久化、调度。"""

from pathlib import Path

import pytest

from dsh_runtime import (
    BotSession, Profile, ProfileError, load_profile, run_once,
)

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


@pytest.fixture(autouse=True)
def clean_store():
    from dsh_runtime import reset
    reset()
    yield
    reset()


def test_load_real_profiles():
    for name in ("crypto-bot", "market-chief", "a-stock-bot", "strategy-lab"):
        profile = load_profile(PROFILES / name / "profile.yaml")
        assert profile.name == name


def test_profile_rejects_undeclared_and_prohibited_tools():
    profile = Profile(
        name="t", description="", market="CRYPTO",
        primary_tools=frozenset({"query_signals"}),
        prohibited=frozenset({"direct_order"}),
    )
    profile.allow("query_signals")
    with pytest.raises(ProfileError):
        profile.allow("preview_order")  # 未声明
    with pytest.raises(ProfileError):
        profile.allow("direct_order")  # 被禁止


def test_memory_and_events_roundtrip():
    session = BotSession.for_profile(Profile(
        name="t", description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))
    session.memory.remember("信号 s-1 已处理", tags=["signal:s-1"])
    assert session.memory.has_tagged("signal:s-1")
    assert not session.memory.has_tagged("signal:s-2")

    session.events.emit("approval/requested", "CRYPTO", "bot", "t",
                        {"approval_id": "appr-1"})
    events = session.events.query("approval/requested")
    assert len(events) == 1
    assert events[0]["payload"]["approval_id"] == "appr-1"
    assert events[0]["actor"] == {"kind": "bot", "id": "t"}


class FlakyAgent:
    name = "flaky"
    calls = 0

    def tick(self, session):
        FlakyAgent.calls += 1
        raise RuntimeError("upstream down")


def test_run_once_swallows_agent_failure_and_records_event():
    session = BotSession.for_profile(Profile(
        name="flaky-host", description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))
    run_once(session, FlakyAgent())  # 不抛
    assert FlakyAgent.calls == 1
    failed = session.events.query("bot/tick.failed")
    assert len(failed) == 1
    assert "upstream down" in failed[0]["payload"]["error"]
    assert session.events.query("bot/tick.finished")
