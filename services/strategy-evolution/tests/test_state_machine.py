from datetime import UTC, datetime

import pytest
from dsh_contracts import Market, StrategyCandidate, StrategyStage

from strategy_evolution.state_machine import PromotionError, promote


def make_candidate(stage: StrategyStage = StrategyStage.DRAFT) -> StrategyCandidate:
    return StrategyCandidate(
        candidate_id="cand-1",
        market=Market.A_SHARE,
        strategy_id="strat-1",
        strategy_version="0.1.0",
        stage=stage,
        updated_at=datetime.now(UTC),
    )


def test_normal_promotion_chain():
    c = make_candidate()
    refs = ["e1", "e2", "e3"]
    c = promote(c, StrategyStage.BACKTESTED, refs[:1])
    c = promote(c, StrategyStage.VALIDATED, refs[:2])
    c = promote(c, StrategyStage.PAPER, refs)
    assert c.stage == StrategyStage.PAPER


def test_skip_stage_rejected():
    with pytest.raises(PromotionError):
        promote(make_candidate(), StrategyStage.PAPER, ["e1", "e2"])


def test_single_backtest_insufficient_for_validated():
    c = promote(make_candidate(), StrategyStage.BACKTESTED, ["only-one"])
    with pytest.raises(PromotionError):
        promote(c, StrategyStage.VALIDATED, ["only-one"])


def test_approved_requires_approval_id():
    c = make_candidate(StrategyStage.SHADOW)
    with pytest.raises(PromotionError):
        promote(c, StrategyStage.APPROVED, ["e1", "e2", "e3"])


def test_production_requires_three_evidence_refs():
    c = make_candidate(StrategyStage.APPROVED, )
    c = promote(c, StrategyStage.CANARY, ["e1", "e2", "e3"], approval_id="appr-1")
    c = promote(c, StrategyStage.PRODUCTION, ["e4", "e5", "e6"], approval_id="appr-1")
    assert c.stage == StrategyStage.PRODUCTION


def test_rollback_from_any_stage():
    c = make_candidate(StrategyStage.PAPER)
    c = promote(c, StrategyStage.ROLLED_BACK, [])
    assert c.stage == StrategyStage.ROLLED_BACK
    with pytest.raises(PromotionError):
        promote(c, StrategyStage.SHADOW, ["e1", "e2"])
