"""Market Chief 总控插件集成测试。

真实链路：DSH Session → MarketChiefAgent tick → Quant Gateway（TestClient）→
跨市场健康检查、状态卡片、待审批待办、降级 incident 事件。全程无真实交易所。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class _State:
    """跨测试共享的可调假适配器。"""
    def __init__(self):
        self.health = {
            Market.A_SHARE: self._ok(Market.A_SHARE),
            Market.CRYPTO: self._ok(Market.CRYPTO),
        }
        self.accounts = {
            Market.A_SHARE: [self._acct(Market.A_SHARE)],
            Market.CRYPTO: [self._acct(Market.CRYPTO)],
        }

    @staticmethod
    def _ok(m: Market) -> HealthStatus:
        return HealthStatus(
            market=m, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    @staticmethod
    def _acct(m: Market) -> AccountSummary:
        return AccountSummary(
            market=m, account_id=f"{m.value.lower()}-paper-1",
            cash="10000", equity="10000", currency="CNY",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )


class ChiefFakeAdapter(MarketAdapter):
    """共享状态假适配器：Market Chief 调度读，专业 Bot 写。"""
    state: _State = _State()

    def __init__(self, market: Market):
        self.market = market

    def get_health(self) -> HealthStatus:
        return self.state.health[self.market]

    def get_positions(self, account_id=None):
        return []

    def get_account_summary(self):
        return self.state.accounts[self.market]

    def get_signals(self):
        return [Signal(
            signal_id="sig-001", market=self.market,
            strategy_id="trend-momentum", strategy_version="1.2.0",
            symbol="BTC/USDT" if self.market == Market.CRYPTO else "600519.SH",
            side="BUY", strength=0.8,
            generated_at=datetime.now(UTC),
            valid_until=datetime.now(UTC) + timedelta(minutes=30),
            data_snapshot_id="snap-1",
        )]

    def preview_order(self, intent):
        return OrderPreview(
            intent=intent, estimated_cost="650", estimated_slippage="0.5",
            risk=RiskSnapshot(
                risk_snapshot_id="rs-preview", market=self.market,
                account_id="paper-1",
                position_before="0", position_after="0.01",
                risk_budget_delta="6.5", worst_case_loss="6.5",
                as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        raise AssertionError("Market Chief 不允许直接下单")

    def get_order_status(self, order_id):
        return {"order_id": order_id, "status": "FILLED"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id):
        pass

    def resume_strategy(self, strategy_id):
        pass

    def emergency_stop(self, account_id=None):
        pass


@pytest.fixture(autouse=True)
def setup_gateway():
    from quant_gateway import approval_store

    approval_store.reset()
    ChiefFakeAdapter.state = _State()
    register_adapter(Market.A_SHARE, ChiefFakeAdapter(Market.A_SHARE))
    register_adapter(Market.CRYPTO, ChiefFakeAdapter(Market.CRYPTO))
    reset()
    yield
    approval_store.reset()
    reset()


def _agent_and_session():
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client

    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client

    from dsh_market_chief import MarketChiefAgent
    agent = MarketChiefAgent(gateway=gateway, approvals=approvals)
    profile = load_profile(PROFILES / "market-chief" / "profile.yaml")
    return agent, BotSession.for_profile(profile)


def test_tick_produces_cross_market_summary():
    agent, session = _agent_and_session()
    run_once(session, agent)

    memos = session.memory.recent(kind="market-summary")
    assert len(memos) == 1
    summary = memos[0]["content"]
    assert "A_SHARE" in summary
    assert "CRYPTO" in summary
    assert "正常" in summary  # 两市场都健康


def test_degraded_market_emits_incident():
    # 把 CRYPTO 标记为降级
    ChiefFakeAdapter.state.health[Market.CRYPTO] = HealthStatus(
        market=Market.CRYPTO, system_ok=False, data_fresh=True,
        trading_channel_ok=True, clock_skew_ms=0,
        as_of=datetime.now(UTC),
    )
    agent, session = _agent_and_session()
    run_once(session, agent)

    incidents = session.events.query("incident/opened")
    assert len(incidents) == 1
    assert incidents[0]["actor"]["id"] == "market-chief"
    assert "CRYPTO" in incidents[0]["market"]

    advice = session.memory.recent(kind="advice")
    assert any("降级" in m["content"] for m in advice)


def test_pending_approvals_become_todo():
    # 通过审批接口预先注入一条待审批
    client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "crypto-bot",
        "subject_type": "control_action",
        "subject_id": "pause-strategy-001",
        "evidence_refs": ["signal:sig-001"],
    })

    agent, session = _agent_and_session()
    run_once(session, agent)

    todos = session.memory.recent(kind="todo")
    assert any("1 项待审批" in t["content"] for t in todos)


def test_health_check_failure_is_fail_closed():
    # 用一个会抛错的适配器替换
    class BadAdapter(ChiefFakeAdapter):
        def get_health(self):
            raise RuntimeError("upstream down")

    register_adapter(Market.CRYPTO, BadAdapter(Market.CRYPTO))
    agent, session = _agent_and_session()
    run_once(session, agent)  # 不应外抛

    incidents = session.events.query("incident/opened")
    assert any("CRYPTO" in i["market"] for i in incidents)
    # A 股仍应正常汇总
    summaries = session.memory.recent(kind="market-summary")
    assert "A_SHARE" in summaries[0]["content"]


def test_chief_is_strictly_read_only():
    """只读验收：Market Chief 不产生任何订单/审批/资金动作，只有查询与汇总。"""
    agent, session = _agent_and_session()
    approvals_before = len(client.get("/v1/approvals").json())
    audit_before = len(client.get("/v1/audit").json())

    for _ in range(3):
        run_once(session, agent)

    # 没有创建任何审批
    assert len(client.get("/v1/approvals").json()) == approvals_before
    # 网关审计没有新增任何资金动作（无下单/撤单/控制）
    audit_now = client.get("/v1/audit").json()
    assert len(audit_now) == audit_before
    forbidden = {"order.submitted", "order.cancelled", "strategy.paused",
                 "strategy.resumed", "emergency.stop", "approval.approved",
                 "approval.rejected"}
    assert not forbidden & {e["action"] for e in audit_now}
    # 事件只有汇总与调度，没有订单事件
    order_events = [e for e in session.events.query(limit=100)
                    if e["event_type"].startswith("order/")]
    assert order_events == []
    # 汇总记忆持续产出
    assert len(session.memory.recent(kind="market-summary")) >= 1


def test_chief_summarizes_bot_health_and_incidents():
    """Bot 健康度汇总：从 Runtime 账本（同库只读）聚合 tick 失败与任务状态。"""
    # 注入 crypto-bot 的失败与任务状态（模拟另一 Bot 的账本痕迹）
    from dsh_runtime import BotSession as BS, Profile as PF
    from dsh_runtime import reset
    reset()
    fake = BS.for_profile(PF(
        name="crypto-bot", description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))
    fake.events.emit("bot/tick.failed", "CRYPTO", "system", "crypto-bot",
                     {"bot": "crypto-bot", "error": "upstream down"})
    fake.tasks.create("paper-order", "sig-1", {"signal_id": "sig-1"})
    fake.events.emit("incident/opened", "CRYPTO", "bot", "crypto-bot",
                     {"reason": "test"})

    agent, session = _agent_and_session()
    run_once(session, agent)

    summaries = session.events.query("market/chief.summary")
    assert len(summaries) == 1
    payload = summaries[0]["payload"]
    assert "crypto-bot" in payload["bots"]
    bot = payload["bots"]["crypto-bot"]
    assert bot["tick_failed_recent"] == 1
    assert bot["health"] == "degraded"
    assert bot["tasks"].get("SIGNAL_RECEIVED") == 1
    assert payload["open_incidents"] == 1


def test_chief_health_queries_are_read_only():
    """健康度查询只读 Runtime 账本：不新增任何 Bot 任务或事件。"""
    from dsh_runtime import reset
    from dsh_runtime.store import _get

    reset()
    agent, session = _agent_and_session()
    conn = _get()
    before_tasks = conn.execute("SELECT COUNT(*) FROM bot_tasks").fetchone()[0]
    before_events = conn.execute(
        "SELECT COUNT(*) FROM domain_events WHERE actor_id != 'market-chief'"
    ).fetchone()[0]

    run_once(session, agent)

    assert conn.execute("SELECT COUNT(*) FROM bot_tasks").fetchone()[0] == before_tasks
    after_events = conn.execute(
        "SELECT COUNT(*) FROM domain_events WHERE actor_id != 'market-chief'"
    ).fetchone()[0]
    assert after_events == before_events


def test_chief_forwards_incidents_with_dedupe(monkeypatch):
    """Chief 转发事故到 Incident Center：幂等去重，中心不可达不影响汇总。"""

    from dsh_runtime import BotSession as BS, Profile as PF, reset
    from fastapi.testclient import TestClient as TC

    reset()
    # 注入两条同指纹事故 + 一条不同指纹
    fake_bot = BS.for_profile(PF(
        name="crypto-bot", description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))
    fake_bot.events.emit("incident/opened", "CRYPTO", "bot", "crypto-bot",
                         {"reason": "order UNKNOWN beyond quarantine",
                          "order_id": "ord-1"})
    fake_bot.events.emit("incident/opened", "CRYPTO", "bot", "crypto-bot",
                         {"reason": "order UNKNOWN beyond quarantine",
                          "order_id": "ord-1"})
    fake_bot.events.emit("incident/opened", "CRYPTO", "bot", "crypto-bot",
                         {"reason": "fill/position mismatch after FILLED",
                          "order_id": "ord-2"})

    from incident_center import main as ic
    ic.reset()
    ic_client = TC(ic.app)

    posted = []

    class FakeIC:
        def post(self, path, json=None, timeout=None):
            posted.append(json)
            class R:
                status_code = 201
                def json(self):
                    return {"incident_id": "inc-x", "occurrences": 1}
            return R()

    monkeypatch.setattr(
        "dsh_market_chief.chief.httpx", type("H", (), {"HTTPError": Exception,
                                                       "post": staticmethod(
                                                           lambda *a, **k: FakeIC().post(*a, **k))}))
    monkeypatch.setenv("INCIDENT_CENTER_URL", "http://ic.test")

    agent, session = _agent_and_session()
    run_once(session, agent)   # 第一次：转发 3 条
    run_once(session, agent)   # 第二次：重复转发（中心侧幂等去重）

    assert len(posted) == 6  # 每次 tick 转发全部未决（幂等由中心保证）
    # 指纹语义验证：用真实中心核对
    ic.reset()
    for body in posted[:3]:
        ic_client.post("/v1/incidents", json=body)
    incidents = ic_client.get("/v1/incidents").json()
    assert len(incidents) == 2  # ord-1 两条合并，ord-2 独立
    assert incidents[0]["occurrences"] + incidents[1]["occurrences"] == 3
    ic.reset()


def test_chief_forward_failure_does_not_break_summary(monkeypatch):
    """Incident Center 不可达：Chief 记录错误记忆，汇总照常输出。"""

    class Unreachable:
        def post(self, *a, **k):
            raise __import__("httpx").HTTPError("connection refused")

    import dsh_market_chief.chief as chief_mod
    monkeypatch.setattr(chief_mod.httpx, "post",
                        lambda *a, **k: Unreachable().post(*a, **k))
    monkeypatch.setenv("INCIDENT_CENTER_URL", "http://ic-down.test")

    agent, session = _agent_and_session()
    run_once(session, agent)
    summaries = session.events.query("market/chief.summary")
    assert len(summaries) == 1  # 汇总未被事故转发失败破坏
