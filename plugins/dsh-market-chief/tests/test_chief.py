"""Market Chief 汇总测试：只读查询、降级告警、审批计数。"""

from datetime import UTC, datetime

import pytest
from dsh_contracts import HealthStatus, Market
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, Profile, load_profile, run_once, reset
from pathlib import Path
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway import approval_store

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


class ChiefFakeAdapter(MarketAdapter):
    def __init__(self, market, healthy=True):
        self.market, self.healthy = market, healthy

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=self.healthy, data_fresh=self.healthy,
            trading_channel_ok=True, clock_skew_ms=0,
            degraded=not self.healthy, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None): return []
    def get_account_summary(self): return []
    def get_signals(self): return []
    def preview_order(self, intent): return {}
    def request_order(self, intent):
        raise AssertionError("Chief 不允许下单")
    def get_order_status(self, order_id): return {}
    def cancel_order(self, order_id): return {}
    def pause_strategy(self, sid): pass
    def resume_strategy(self, sid): pass
    def emergency_stop(self, account_id=None): pass


@pytest.fixture(autouse=True)
def setup():
    approval_store.reset()
    reset()
    yield
    approval_store.reset()
    reset()


def _chief_and_session():
    from fastapi.testclient import TestClient
    from quant_gateway.main import app
    from dsh_market_chief import MarketChief

    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = TestClient(app)
    profile = load_profile(PROFILES / "market-chief" / "profile.yaml")
    return MarketChief(gateway=gateway), BotSession.for_profile(profile)


def test_chief_summarizes_health_and_pending_approvals():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=True))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))
    approval_store.create_approval(
        market=Market.CRYPTO, requested_by_bot="crypto-bot",
        subject_type="order", subject_id="s1",
    )

    chief, session = _chief_and_session()
    run_once(session, chief)

    summaries = session.events.query("market/chief.summary")
    assert len(summaries) == 1
    assert summaries[0]["payload"]["pending_approvals"] == 1
    assert summaries[0]["payload"]["degraded"] == []
    assert session.events.query("incident/opened") == []


def test_chief_alerts_on_degraded_market():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=False))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))

    chief, session = _chief_and_session()
    run_once(session, chief)

    incidents = session.events.query("incident/opened")
    assert len(incidents) == 1
    assert "A_SHARE" in incidents[0]["payload"]["markets"]


def test_chief_writes_daily_briefing():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=True))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))
    from dsh_runtime.store import _get

    conn = _get()
    conn.execute(
        "INSERT INTO bot_tasks"
        " (task_id, bot, kind, status, subject_id, payload, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task-crypto-sig-1",
            "crypto-bot",
            "shadow-decision",
            "SHADOW_RECORDED",
            "sig-1",
            '{"market":"CRYPTO","symbol":"BTCUSDT","side":"BUY","shadow_decision":'
            '{"action":"BUY","strength":0.9,"disclaimer":"仅模拟，不会下单"}}',
            "2026-08-19T00:00:00+00:00",
            "2026-08-19T00:00:00+00:00",
        ),
    )
    conn.commit()
    chief, session = _chief_and_session()
    run_once(session, chief)
    briefing = session.events.query("market/chief.briefing")
    assert briefing
    assert briefing[0]["payload"]["counts"]["execute"] == 1
    notes = session.memory.recent(kind="daily-briefing")
    assert notes


def test_chief_briefing_includes_intelligence_and_audits():
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE, healthy=True))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO, healthy=True))
    from dsh_runtime.store import _get

    conn = _get()
    conn.execute(
        "INSERT INTO intelligence_items"
        " (item_id, bot, market, source_id, symbol, title, observed_at,"
        " importance, confidence, action, dedupe_key, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "intel-1",
            "crypto-bot",
            "CRYPTO",
            "eth-foundation",
            "ETHUSDT",
            "Ethereum 基金会发布路线调整",
            "2026-08-20T00:00:00+00:00",
            0.82,
            0.72,
            "SELL",
            "dedupe-1",
            '{"title":"Ethereum 基金会发布路线调整"}',
        ),
    )
    conn.execute(
        "INSERT INTO audit_reports"
        " (report_id, bot, market, report_kind, period_key, created_at, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "report-1",
            "crypto-bot",
            "CRYPTO",
            "intelligence-daily",
            "2026-08-20",
            "2026-08-20T00:10:00+00:00",
            '{"score":{"intelligence_hit_rate":0.75}}',
        ),
    )
    conn.commit()
    chief, session = _chief_and_session()
    run_once(session, chief)
    briefing = session.events.query("market/chief.briefing")[0]["payload"]
    assert briefing["counts"]["intelligence"] >= 1
    assert briefing["intelligence"][0]["symbol"] == "ETHUSDT"
    assert briefing["focus_today"]
    assert briefing["audits"][0]["bot"] == "crypto-bot"
