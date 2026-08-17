"""Risk Auditor 独立 HTTP 服务。

与 strategy-evolution / quant-gateway 进程隔离：经 REST 调用，
不可达或拒绝时由调用方失败关闭。禁止在 evolution 进程内直接调库绕过边界。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from dsh_contracts import Market, RiskSnapshot, StrategyCandidate
from dsh_risk_auditor.auditor import RiskAuditor
from dsh_risk_auditor import storage

app = FastAPI(
    title="DSH Risk Auditor",
    description="独立风控审计服务：策略晋级与订单二次校验边界。",
    version="0.1.0",
)

_auditor = RiskAuditor()


class PromotionAuditRequest(BaseModel):
    candidate: dict
    evidence_refs: list[str] = Field(default_factory=list)
    upstream_passed: bool = True


class OrderAuditRequest(BaseModel):
    market: str
    risk_snapshot: dict
    equity: str
    upstream_passed: bool = True


def _now() -> datetime:
    return datetime.now(UTC)


def _evidence_hash(evidence_refs: list[str]) -> str:
    canonical = json.dumps(sorted(evidence_refs), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk-auditor"}


@app.post("/v1/audit-promotion")
def audit_promotion(req: PromotionAuditRequest) -> dict:
    try:
        candidate = StrategyCandidate.model_validate(req.candidate)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid candidate: {exc}") from exc

    verdict = _auditor.evaluate_promotion(
        candidate=candidate,
        evidence_refs=req.evidence_refs,
        upstream_passed=req.upstream_passed,
    )
    evidence_hash = _evidence_hash(req.evidence_refs)
    record = {
        "audit_id": f"aud-{uuid4().hex[:16]}",
        "candidate_id": candidate.candidate_id,
        "strategy_id": candidate.strategy_id,
        "strategy_version": candidate.strategy_version,
        "evidence_hash": evidence_hash,
        "evidence_refs": list(req.evidence_refs),
        "approved": verdict.approved,
        "reason": verdict.reason,
        "audited_at": _now().isoformat(),
    }
    storage.save_promotion_audit(record)
    return record


@app.get("/v1/audits/{audit_id}")
def get_audit(audit_id: str) -> dict:
    record = storage.get_promotion_audit(audit_id)
    if record is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return record


@app.post("/v1/audit-order")
def audit_order(req: OrderAuditRequest) -> dict:
    try:
        market = Market(req.market)
        risk = RiskSnapshot.model_validate(req.risk_snapshot)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid order audit request: {exc}") from exc

    verdict = _auditor.evaluate_order(
        market, risk, req.equity, req.upstream_passed,
    )
    return {
        "audit_id": f"aud-{uuid4().hex[:16]}",
        "approved": verdict.approved,
        "reason": verdict.reason,
        "strategy_version": None,
        "evidence_hash": None,
        "audited_at": _now().isoformat(),
    }
