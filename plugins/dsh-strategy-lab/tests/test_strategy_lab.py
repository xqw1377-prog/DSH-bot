"""Strategy Lab 策略实验室插件测试。

用 httpx.MockTransport 模拟 strategy-evolution 服务，不启动真实服务。
"""

import httpx
import pytest

from dsh_contracts import Market
from dsh_runtime import BotSession, Profile, reset
from dsh_strategy_lab import StrategyLab


@pytest.fixture(autouse=True)
def clean_store():
    reset()
    yield
    reset()


def _session() -> BotSession:
    return BotSession.for_profile(Profile(
        name="strategy-lab", description="", market="GLOBAL",
        primary_tools=frozenset({
            "create_hypothesis", "run_experiment",
            "compare_results", "submit_candidate",
        }),
        prohibited=frozenset(),
    ))


def _make_lab(monkeypatch, handler) -> StrategyLab:
    """构造一个使用 MockTransport 的 StrategyLab。"""
    real_client = httpx.Client
    holder = {"handler": handler}

    def fake_factory(*args, **kwargs):
        def delegate(request: httpx.Request) -> httpx.Response:
            return holder["handler"](request)
        kwargs["transport"] = httpx.MockTransport(delegate)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("dsh_strategy_lab.lab.httpx.Client", fake_factory)
    return StrategyLab(evolution_base_url="http://evolution.test")


def test_propose_creates_experiment_and_event(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/experiments"
        return httpx.Response(201, json={
            "experiment_id": "exp-001",
            "market": "CRYPTO",
            "strategy_id": "trend-momentum",
        })

    lab = _make_lab(monkeypatch, handler)
    s = _session()
    exp_id = lab.propose(
        s, Market.CRYPTO, "trend-momentum",
        "动量策略在低波动率环境下表现更好", "snap-1",
    )
    assert exp_id == "exp-001"
    assert s.events.query("strategy/hypothesis.created")
    assert s.memory.has_tagged("experiment:exp-001")


def test_propose_handles_upstream_failure(monkeypatch):
    def handler(req):
        return httpx.Response(500, text="evolution down")

    lab = _make_lab(monkeypatch, handler)
    s = _session()
    assert lab.propose(s, Market.A_SHARE, "s", "h", "d") is None
    errors = s.memory.recent(kind="error")
    assert any("创建实验失败" in e["content"] for e in errors)


def test_submit_candidate_creates_event(monkeypatch):
    def handler(req):
        if req.url.path == "/v1/candidates":
            return httpx.Response(201, json={
                "candidate_id": "cand-1",
                "market": "CRYPTO",
                "strategy_id": "trend-momentum",
            })
        return httpx.Response(404)

    lab = _make_lab(monkeypatch, handler)
    s = _session()
    cand_id = lab.submit_candidate(
        s, "exp-1", Market.CRYPTO, "trend-momentum", "1.0.0",
        ["backtest:1", "paper:2", "shadow:3"],
    )
    assert cand_id == "cand-1"
    events = s.events.query("candidate/nominated")
    assert events[0]["payload"]["evidence_refs"] == [
        "backtest:1", "paper:2", "shadow:3"
    ]


def test_tick_advises_on_experiments_with_results(monkeypatch):
    def handler(req):
        return httpx.Response(200, json=[{
            "experiment_id": "exp-1",
            "strategy_id": "s-1",
            "result_ref": "backtest-2026-01",
            "market": "CRYPTO",
        }])

    lab = _make_lab(monkeypatch, handler)
    s = _session()
    lab.tick(s)
    advice = s.memory.recent(kind="advice")
    assert any("exp-1" in a["content"] for a in advice)

    # 第二次 tick 不应重复提示
    lab.tick(s)
    advice = s.memory.recent(kind="advice")
    assert sum(1 for a in advice if "exp-1" in a["content"]) == 1


def test_tick_handles_upstream_failure_gracefully(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("evolution down")

    lab = _make_lab(monkeypatch, handler)
    s = _session()
    lab.tick(s)  # 不应外抛
    assert s.memory.recent(kind="error")


def test_lab_cannot_deploy_to_production():
    """红线：lab 不应提供任何晋级到 PRODUCTION 的方法。"""
    lab = StrategyLab.__new__(StrategyLab)
    # 确认公开方法里没有 promote/deploy 类入口
    public = [m for m in dir(lab) if not m.startswith("_") and m != "close"]
    assert not any("promote" in m or "deploy" in m or "approve" in m for m in public)
