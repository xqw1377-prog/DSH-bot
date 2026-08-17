"""独立风控审计服务（Risk Auditor）。

职责：策略晋级前的独立复核——与 strategy-evolution 进程、存储、代码
边界全部分离；只有只读证据输入，没有任何交易凭据或账户访问。

审计规则（独立于状态机，双门禁）：
- 证据哈希必须与证据列表重新计算一致（防篡改）
- 晋级到 APPROVED/CANARY/PRODUCTION 至少 3 条不同证据
  （策略晋级不能只依据单次回测，独立计数）
- 必须携带人工 approval_id
结论按（candidate, to_stage, evidence_hash）幂等：重复请求返回同一结论。
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from risk_auditor import storage

# 与 strategy-evolution 状态机的最少证据数一致，但独立计数
MIN_EVIDENCE_REFS = 3
STAGES_REQUIRING_APPROVAL = {"APPROVED", "CANARY", "PRODUCTION"}

app = FastAPI(
    title="Risk Auditor",
    description="独立风控审计：只读证据、无交易凭据、结论绑定候选与证据哈希。",
    version="0.1.0",
)


class AuditPromotionRequest(BaseModel):
    candidate_id: str
    market: str
    strategy_id: str
    strategy_version: str
    from_stage: str
    to_stage: str
    evidence_refs: list[str]
    evidence_hash: str
    approval_id: str | None = None


def _hash_matches(refs: list[str], claimed: str) -> bool:
    return storage.evidence_hash(refs) == claimed


@app.post("/v1/audit-promotion")
def audit_promotion(req: AuditPromotionRequest) -> dict:
    # 幂等：同一结论直接返回
    existing = storage.find_conclusion(
        req.candidate_id, req.to_stage, req.evidence_hash)
    if existing is not None:
        return {**existing, "idempotent": True}

    verdict, reason = _evaluate(req)

    conclusion = storage.save_conclusion(
        req.candidate_id, req.to_stage, req.evidence_hash,
        verdict, reason, req.strategy_version,
    )
    return {**conclusion, "idempotent": False}


def _evaluate(req: AuditPromotionRequest) -> tuple[str, str]:
    if not _hash_matches(req.evidence_refs, req.evidence_hash):
        return "FAIL", "evidence hash mismatch: refs do not hash to claimed value"
    distinct = sorted(set(req.evidence_refs))
    if req.to_stage in STAGES_REQUIRING_APPROVAL:
        if len(distinct) < MIN_EVIDENCE_REFS:
            return "FAIL", (
                f"insufficient independent evidence: {len(distinct)} < "
                f"{MIN_EVIDENCE_REFS} for {req.to_stage}")
        if not req.approval_id:
            return "FAIL", "missing human approval_id for " + req.to_stage
    return "PASS", (
        f"evidence hash verified with {len(distinct)} distinct refs; "
        f"approval bound")


@app.get("/v1/conclusions/{candidate_id}")
def conclusions(candidate_id: str) -> list[dict]:
    with storage.locked_conn() as conn:
        rows = conn.execute(
            "SELECT conclusion_id, to_stage, evidence_hash, verdict, reason,"
            " strategy_version, created_at FROM conclusions"
            " WHERE candidate_id = ?", (candidate_id,)).fetchall()
    keys = ("conclusion_id", "to_stage", "evidence_hash", "verdict",
            "reason", "strategy_version", "created_at")
    return [dict(zip(keys, r)) for r in rows]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "risk-auditor"}
