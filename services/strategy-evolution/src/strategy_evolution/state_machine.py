"""策略晋级状态机与验证门禁，见 PRD 10.4 与附录 A.1。

规则：
- 只允许沿主链 DRAFT -> BACKTESTED -> VALIDATED -> PAPER -> SHADOW
  -> APPROVED -> CANARY -> PRODUCTION 单步推进
- 每次推进必须附带证据引用（evidence_refs），单次回测不足以晋级
- 进入 APPROVED 及之后的阶段必须已有人工审批 ID（approval_id）
- RETIRED / ROLLED_BACK 为终态，可从任意非终态进入
"""

from datetime import UTC, datetime

from dsh_contracts import StrategyCandidate, StrategyStage


def _now() -> datetime:
    return datetime.now(UTC)

# 主链晋级路径（单步）
_PROMOTION_PATH: dict[StrategyStage, StrategyStage] = {
    StrategyStage.DRAFT: StrategyStage.BACKTESTED,
    StrategyStage.BACKTESTED: StrategyStage.VALIDATED,
    StrategyStage.VALIDATED: StrategyStage.PAPER,
    StrategyStage.PAPER: StrategyStage.SHADOW,
    StrategyStage.SHADOW: StrategyStage.APPROVED,
    StrategyStage.APPROVED: StrategyStage.CANARY,
    StrategyStage.CANARY: StrategyStage.PRODUCTION,
}

# 进入这些阶段之前必须存在人工审批
_STAGES_REQUIRING_APPROVAL = {
    StrategyStage.APPROVED,
    StrategyStage.CANARY,
    StrategyStage.PRODUCTION,
}

# 各阶段晋级所需的最少证据条数（策略晋级不能只依据单次回测）
_MIN_EVIDENCE_REFS: dict[StrategyStage, int] = {
    StrategyStage.BACKTESTED: 1,
    StrategyStage.VALIDATED: 2,
    StrategyStage.PAPER: 2,
    StrategyStage.SHADOW: 2,
    StrategyStage.APPROVED: 3,
    StrategyStage.CANARY: 3,
    StrategyStage.PRODUCTION: 3,
}

_TERMINAL_STAGES = {StrategyStage.RETIRED, StrategyStage.ROLLED_BACK}


class PromotionError(ValueError):
    """晋级请求不满足门禁。"""


def promote(candidate: StrategyCandidate, target: StrategyStage,
            evidence_refs: list[str], approval_id: str | None = None) -> StrategyCandidate:
    """校验并返回推进到 target 阶段的新候选（不可变更新）。"""
    if candidate.stage in _TERMINAL_STAGES:
        raise PromotionError(f"candidate is terminal at {candidate.stage}")

    if target in _TERMINAL_STAGES:
        # 退役/回滚允许从任意非终态进入
        return candidate.model_copy(update={
            "stage": target,
            "evidence_refs": candidate.evidence_refs + evidence_refs,
            "updated_at": _now(),
        })

    if _PROMOTION_PATH.get(candidate.stage) != target:
        raise PromotionError(
            f"illegal transition {candidate.stage} -> {target}; "
            f"expected {_PROMOTION_PATH.get(candidate.stage)}"
        )

    min_refs = _MIN_EVIDENCE_REFS.get(target, 1)
    if len(evidence_refs) < min_refs:
        raise PromotionError(
            f"promotion to {target} requires at least {min_refs} evidence refs, "
            f"got {len(evidence_refs)}"
        )

    if target in _STAGES_REQUIRING_APPROVAL and not approval_id:
        raise PromotionError(f"promotion to {target} requires an approval_id")

    return candidate.model_copy(update={
        "stage": target,
        "evidence_refs": candidate.evidence_refs + evidence_refs,
        "approval_id": approval_id or candidate.approval_id,
        "updated_at": _now(),
    })
