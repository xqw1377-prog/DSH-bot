"""交易质量审计闭环验收(阶段 4)。

验收链路:闭环成交导入 → 回合关闭 + 六维审计 + 偏差警告 →
优化管线(重放/回测)阶段推进 → 每日/每周报告 → Shadow 候选
提名到 strategy-evolution(只注册 DRAFT,不晋级)。

红线:全程 can_apply=False / trade_blocked=True,禁止直接改运行策略。
"""

from pathlib import Path

import httpx
import pytest

from dsh_runtime import BotSession, load_profile, reset
from dsh_runtime.trade_audit import (
    deviation_warnings,
    ingest_and_audit_trades,
    nominate_candidate_to_evolution,
)

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


def _session():
    reset()
    return BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))


def _trade(tid, *, pnl="40", pnl_r="0.8", fee="0.4", hold_hours=3.0,
           closed="2026-08-19T12:00:00+00:00"):
    from datetime import datetime, timedelta

    closed_at = datetime.fromisoformat(closed)
    opened_at = (closed_at - timedelta(hours=hold_hours)).isoformat()
    return {
        "symbol": "ETHUSDT", "side": "SELL", "source_trade_id": tid,
        "entry_price": "2000", "exit_price": "1900",
        "pnl": pnl, "pnl_r": pnl_r, "fee": fee,
        "exit_reason": "移动止盈", "signal_type": "V5",
        "opened_at": opened_at, "closed_at": closed,
    }


# ---- 1. 偏差警告 ----

def test_deviation_warnings_fee_drag():
    warnings = deviation_warnings(_trade("w1", pnl="10", fee="5"))
    assert any("手续费拖累" in w for w in warnings)


def test_deviation_warnings_late_stop_and_oversized_loss():
    warnings = deviation_warnings(
        _trade("w2", pnl="-50", pnl_r="-2.5", hold_hours=60))
    assert any("止损过晚" in w for w in warnings)
    assert any("2R" in w for w in warnings)


def test_deviation_warnings_clean_trade():
    assert deviation_warnings(_trade("w3")) == []


def test_warnings_attached_to_audit_and_daily_report():
    session = _session()
    result = ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade("f1", pnl="10", fee="5"),
    ])
    assert result["imported"] == 1
    # 审计附件带警告
    row = session.ledger.list()[0]
    assert "手续费拖累" in " ".join(row["payload"]["audit"]["warnings"])
    # 结果与每日报告都带警告
    assert result["warnings"] and result["warnings"][0]["fill_id"] == "f1"
    daily = session.reports.list(report_kind="optimization-daily", limit=1)[0]
    assert daily["payload"]["warnings"]
    # 每周汇总已生成
    weekly = session.reports.list(report_kind="optimization-weekly", limit=1)[0]
    assert weekly["payload"]["days_covered"] >= 1


def test_losing_streak_warning():
    session = _session()
    result = ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade(f"l{i}", pnl="-10", pnl_r="-0.5") for i in range(3)
    ])
    assert any(
        "连续亏损" in w["warnings"][0]
        for w in result["warnings"] if w["fill_id"] is None
    )


# ---- 2. 管线阶段推进(数据增多 → SUGGESTION → SHADOW) ----

def test_pipeline_progresses_with_more_data():
    session = _session()
    small = ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade(f"s{i}", pnl="10", pnl_r="0.5") for i in range(3)
    ])
    # 小样本/无改善:候选停留在早期阶段
    assert all(c["stage"] != "SHADOW" for c in small["candidates"])

    from datetime import datetime, timedelta

    base = datetime.fromisoformat("2026-08-19T12:00:00+00:00")
    favorable = []
    for i in range(12):
        loser = i % 2 == 0  # 交替亏赢单,保证样本外切片两类都有
        favorable.append(_trade(
            f"g{i}",
            pnl="-100" if loser else "30",
            pnl_r="-3.5" if loser else "0.6",
            closed=(base + timedelta(hours=i)).isoformat(),
        ))
    big = ingest_and_audit_trades(session, market="CRYPTO", trades=favorable)
    shadowed = [c for c in big["candidates"] if c["stage"] == "SHADOW"]
    assert shadowed, "止损规则在足够样本 + 样本外为正后应推进到 SHADOW"
    for cand in big["candidates"]:
        assert cand["can_apply"] is False
        assert cand["trade_blocked"] is True
    # 候选持久化(稳定 ID)
    saved = session.ledger.list_candidates(market="CRYPTO")
    assert any(row["stage"] == "SHADOW" for row in saved)


# ---- 3. 提名桥(只注册 DRAFT,不晋级) ----

def _shadow_candidate_id(session):
    for row in session.ledger.list_candidates(market="CRYPTO"):
        if row["stage"] == "SHADOW":
            return row["candidate_id"]
    raise AssertionError("no SHADOW candidate")


def _seed_shadow_candidate(session):
    ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade(f"g{i}", pnl="-100" if i % 2 == 0 else "30",
               pnl_r="-3.5" if i % 2 == 0 else "0.6")
        for i in range(12)
    ])
    return _shadow_candidate_id(session)


def test_nomination_registers_draft_in_evolution(monkeypatch):
    session = _session()
    candidate_id = _seed_shadow_candidate(session)

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = httpx.Response(201, json={"candidate_id": "cand-evo-1",
                                         "stage": "DRAFT"})
        resp.request = httpx.Request("POST", url)
        return resp

    monkeypatch.setattr("dsh_runtime.trade_audit.httpx.post", fake_post)
    result = nominate_candidate_to_evolution(
        session, candidate_id=candidate_id,
        evolution_url="http://evolution.test", api_key="evo-key")
    assert result["already_nominated"] is False
    assert result["evolution_candidate_id"] == "cand-evo-1"
    assert captured["url"].endswith("/v1/candidates")
    assert captured["headers"] == {"X-API-Key": "evo-key"}
    assert captured["json"]["market"] == "CRYPTO"

    # 幂等:第二次不再调用
    calls = {"n": 0}

    def counting_post(url, **kw):
        calls["n"] += 1
        raise AssertionError("should not be called")

    monkeypatch.setattr("dsh_runtime.trade_audit.httpx.post", counting_post)
    again = nominate_candidate_to_evolution(
        session, candidate_id=candidate_id, evolution_url="http://evolution.test")
    assert again["already_nominated"] is True
    assert calls["n"] == 0


def test_nomination_refuses_non_shadow(monkeypatch):
    session = _session()
    ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade(f"s{i}", pnl="10", pnl_r="0.5") for i in range(3)
    ])
    early = next(
        row["candidate_id"] for row in session.ledger.list_candidates(market="CRYPTO")
        if row["stage"] != "SHADOW"
    )
    monkeypatch.setattr(
        "dsh_runtime.trade_audit.httpx.post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    with pytest.raises(ValueError, match="SHADOW"):
        nominate_candidate_to_evolution(
            session, candidate_id=early, evolution_url="http://evolution.test")


def test_nomination_fail_closed_on_http_error(monkeypatch):
    session = _session()
    candidate_id = _seed_shadow_candidate(session)

    def error_post(url, **kw):
        resp = httpx.Response(500, text="boom")
        resp.request = httpx.Request("POST", url)
        return resp

    monkeypatch.setattr("dsh_runtime.trade_audit.httpx.post", error_post)
    with pytest.raises(RuntimeError, match="nomination failed"):
        nominate_candidate_to_evolution(
            session, candidate_id=candidate_id, evolution_url="http://evolution.test")


# ---- 4. 闭环红线 ----

def test_closed_loop_never_allows_apply():
    """全程 can_apply=False / trade_blocked=True:审计只出建议。"""
    session = _session()
    result = ingest_and_audit_trades(session, market="CRYPTO", trades=[
        _trade(f"g{i}", pnl="-100" if i % 2 == 0 else "30",
               pnl_r="-3.5" if i % 2 == 0 else "0.6")
        for i in range(12)
    ])
    assert result["can_apply"] is False
    assert result["trade_blocked"] is True
    for row in session.ledger.list_candidates(market="CRYPTO"):
        # list_candidates 返回扁平化行
        assert row.get("can_apply") is False
    weekly = session.reports.list(report_kind="optimization-weekly", limit=1)[0]
    assert weekly["payload"]["can_apply"] is False
