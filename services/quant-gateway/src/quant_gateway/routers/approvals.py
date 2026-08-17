"""Quant Gateway 审批接口。

审批是资金动作与策略晋级的人为门禁（PRD 11.1）。
- Bot 只能创建审批请求（REQUESTED），不能替人决定
- 决定仅允许 REQUESTED -> APPROVED / REJECTED，不可翻转
- 超时未决的审批自动过期（EXPIRED）
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import ApprovalStatus, Market
from quant_gateway import approval_store, audit
from quant_gateway.auth import Principal, auth_enabled, require_read, require_write

router = APIRouter()


class ApprovalCreate(BaseModel):
    market: Market
    requested_by_bot: str
    subject_type: str  # order | strategy_promotion | risk_budget | control_action
    subject_id: str
    evidence_refs: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    decision: ApprovalStatus
    decided_by: str


@router.get("/approvals")
def list_approvals(
    status: ApprovalStatus | None = None,
    market: Market | None = None,
    principal: Principal = Depends(require_read),
):
    _ = principal
    return [
        a.model_dump(mode="json")
        for a in approval_store.list_approvals(status=status, market=market)
    ]


@router.post("/approvals", status_code=201)
def create_approval(req: ApprovalCreate,
                    principal: Principal = Depends(require_write)):
    approval = approval_store.create_approval(
        market=req.market,
        requested_by_bot=req.requested_by_bot,
        subject_type=req.subject_type,
        subject_id=req.subject_id,
        evidence_refs=req.evidence_refs,
    )
    audit.record("approval.requested", f"bot:{req.requested_by_bot}",
                 market=req.market.value, subject_id=req.subject_id,
                 detail=f"approval_id={approval.approval_id}")
    return approval.model_dump(mode="json")


@router.get("/approvals/{approval_id}")
def get_approval(
    approval_id: str, principal: Principal = Depends(require_read)
):
    _ = principal
    approval = approval_store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval.model_dump(mode="json")


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, req: ApprovalDecision,
           principal: Principal = Depends(require_write)):
    decided_by = principal.name if auth_enabled() else req.decided_by
    try:
        approval = approval_store.decide_approval(
            approval_id, req.decision, decided_by
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(f"approval.{req.decision.value.lower()}",
                 actor=f"human:{decided_by}", market=approval.market.value,
                 subject_id=approval_id,
                 detail=f"via key '{principal.name}'")
    return approval.model_dump(mode="json")
