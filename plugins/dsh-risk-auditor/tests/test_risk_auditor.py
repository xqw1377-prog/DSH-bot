"""Risk Auditor 独立风控验证插件测试。

不依赖真实 risk-policy 服务，直接构造 RiskSnapshot / StrategyCandidate，
验证审计结论与 incident 事件留痕。
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dsh_contracts import Market, RiskSnapshot, StrategyCandidate, StrategyStage
from dsh_runtime import BotSession, Profile, reset
from dsh_risk_auditor import RiskAuditor


@pytest.fixture(autouse=True)
def clean_store():
    reset()
    yield
    reset()


def _session() -> BotSession:
    return BotSession.for_profile(Profile(
        name="risk-auditor", description="", market="GLOBAL",
        primary_tools=frozenset({"audit_order", "audit_promotion"}),
        prohibited=frozenset(),
    ))


def _snapshot(market: Market, worst_loss: str = "100") -> RiskSnapshot:
    return RiskSnapshot(
        risk_snapshot_id="rs-1", market=market, account_id="paper-1",
        position_before=Decimal("0"), position_after=Decimal("1"),
        risk_budget_delta=Decimal("100"), worst_case_loss=Decimal(worst_loss),
        as_of=datetime.now(UTC),
    )


def _candidate(market: Market) -> StrategyCandidate:
    return StrategyCandidate(
        candidate_id="cand-1", market=market,
        strategy_id="s-1", strategy_version="1.0.0",
        stage=StrategyStage.SHADOW, updated_at=datetime.now(UTC),
    )


def test_order_audit_passes_when_consistent():
    auditor = RiskAuditor()
    s = _session()
    # equity=1000000, worst_loss=100 → ratio=0.0001 < 0.01 limit
    v = auditor.audit_order(
        s, Market.A_SHARE, _snapshot(Market.A_SHARE, "100"),
        equity="1000000", upstream_passed=True,
    )
    assert v.approved
    assert s.events.query("incident/opened") == []


def test_order_audit_flags_inconsistency():
    auditor = RiskAuditor()
    s = _session()
    # risk-policy 误判通过，但 auditor 算出超限
    v = auditor.audit_order(
        s, Market.A_SHARE, _snapshot(Market.A_SHARE, "50000"),
        equity="1000000", upstream_passed=True,
    )
    assert v.disputed
    incidents = s.events.query("incident/opened")
    assert len(incidents) == 1
    assert "不一致" in incidents[0]["payload"]["reason"]


def test_order_audit_fail_closed_on_bad_equity():
    auditor = RiskAuditor()
    s = _session()
    v = auditor.audit_order(
        s, Market.CRYPTO, _snapshot(Market.CRYPTO),
        equity="not-a-number", upstream_passed=True,
    )
    assert v.disputed
    assert s.events.query("incident/opened")


def test_promotion_audit_rejects_insufficient_evidence():
    auditor = RiskAuditor()
    s = _session()
    v = auditor.audit_promotion(
        s, _candidate(Market.A_SHARE),
        evidence_refs=["backtest:1", "backtest:2"],
        upstream_passed=True,
    )
    assert v.disputed
    assert "不足" in v.reason


def test_promotion_audit_rejects_homogeneous_evidence():
    """策略晋级不能只依据单次回测：同源证据全部为 backtest:* → 拒绝。"""
    auditor = RiskAuditor()
    s = _session()
    v = auditor.audit_promotion(
        s, _candidate(Market.CRYPTO),
        evidence_refs=["backtest:1", "backtest:2", "backtest:3"],
        upstream_passed=True,
    )
    assert v.disputed
    assert "同源" in v.reason
    assert s.events.query("incident/opened")


def test_promotion_audit_passes_with_diverse_evidence():
    auditor = RiskAuditor()
    s = _session()
    v = auditor.audit_promotion(
        s, _candidate(Market.CRYPTO),
        evidence_refs=["backtest:1", "paper:2", "shadow:3"],
        upstream_passed=True,
    )
    assert v.approved
    assert s.events.query("incident/opened") == []
