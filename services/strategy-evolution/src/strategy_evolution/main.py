"""策略持续进化服务。

实验账本与策略晋级状态机。不能直接修改生产策略：
生产策略变更必须由量化系统在本服务输出晋级结论并经人工审批后执行。

持久化（STRATEGY_EVOLUTION_DB）：实验、候选、证据、审计历史全部落库，
重启不丢。晋级语义：
- 乐观锁：请求携带 expected_version，版本冲突返回 409
- 幂等晋级：同一目标阶段 + 同一证据哈希的重复请求返回当前状态，
  不产生二次迁移
- 证据防篡改：证据 append-only 存储，晋级时校验候选 payload 的
  evidence_refs 与账本一致且哈希匹配，不一致失败关闭
- APPROVED 及之后阶段必须有人工 approval_id（状态机门禁）
- 独立风控审计：晋级到 APPROVED/CANARY/PRODUCTION 时调用 Risk Auditor
  （STRATEGY_EVOLUTION_AUDITOR_URL），不可达/超时/驳回一律失败关闭
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import Depends, APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import Experiment, Market, StrategyCandidate, StrategyStage
from strategy_evolution import storage
from strategy_evolution.state_machine import PromotionError, promote

def _auditor_url() -> str:
    """调用时读取：测试与运维可动态切换，避免模块级状态残留。"""
    return os.environ.get("STRATEGY_EVOLUTION_AUDITOR_URL", "")


def _gateway_url() -> str:
    return os.environ.get("STRATEGY_EVOLUTION_GATEWAY_URL", "")


def _gateway_api_key() -> str:
    return os.environ.get("STRATEGY_EVOLUTION_GATEWAY_API_KEY", "")


def _verify_approval_with_gateway(candidate: StrategyCandidate,
                                  req: "PromotionRequest") -> None:
    """回查 Quant Gateway 审批账本，核验人工审批真实有效。

    状态机只看 approval_id 非空是不够的——任意字符串都能伪造。
    晋级到 APPROVED/CANARY/PRODUCTION 必须满足：
    - 审批在网关账本中真实存在
    - 状态为 APPROVED（未过期/未消费/未被拒绝）
    - subject_type = strategy_promotion 且 subject 绑定本候选
    - 市场与候选一致
    网关不可达或未配置 = 失败关闭，绝不猜测放行。
    """
    base = _gateway_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail=("quant gateway not configured "
                    "(STRATEGY_EVOLUTION_GATEWAY_URL); "
                    "cannot verify approval; fail-closed"),
        )
    headers = (
        {"X-API-Key": _gateway_api_key()} if _gateway_api_key() else None
    )
    try:
        resp = httpx.get(
            f"{base.rstrip('/')}/v1/approvals/{req.approval_id}",
            headers=headers, timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"quant gateway unreachable; cannot verify approval: {exc}",
        ) from exc
    if resp.status_code == 404:
        raise HTTPException(
            status_code=422,
            detail=(f"approval {req.approval_id} not found in gateway ledger; "
                    "promotion rejected"),
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail=(f"quant gateway returned {resp.status_code}; "
                    "cannot verify approval; fail-closed"),
        )
    approval = resp.json()
    problems = []
    if approval.get("status") != "APPROVED":
        problems.append(f"status={approval.get('status')}")
    if approval.get("subject_type") != "strategy_promotion":
        problems.append(f"subject_type={approval.get('subject_type')}")
    if approval.get("subject_id") != candidate.candidate_id:
        problems.append(
            f"subject_id={approval.get('subject_id')} != {candidate.candidate_id}"
        )
    if approval.get("market") != candidate.market.value:
        problems.append(
            f"market={approval.get('market')} != {candidate.market.value}"
        )
    if problems:
        raise HTTPException(
            status_code=422,
            detail=(f"approval {req.approval_id} failed verification "
                    f"({'; '.join(problems)}); promotion rejected"),
        )
# Auditor 属于更强门禁：晋级到这些阶段必须通过独立审计
_STAGES_REQUIRING_AUDITOR = {
    StrategyStage.APPROVED, StrategyStage.CANARY, StrategyStage.PRODUCTION,
}

from strategy_evolution.service_auth import require_service_key

app = FastAPI(
    title="Strategy Evolution",
    description="策略持续进化服务。实验账本、验证门禁、晋级状态机。"
                "不能直接修改生产策略。账本持久化，重启不丢。",
    version="0.3.0",
)


def _now() -> datetime:
    return datetime.now(UTC)


class ExperimentCreate(BaseModel):
    market: Market
    strategy_id: str
    hypothesis: str
    data_snapshot_id: str
    created_by_bot: str = "market-chief"


class CandidateCreate(BaseModel):
    market: Market
    strategy_id: str
    strategy_version: str


class PromotionRequest(BaseModel):
    target_stage: StrategyStage
    evidence_refs: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    expected_version: int | None = None
    idempotency_key: str | None = None


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_service_key)])


# ---- 实验 ----

@v1.get("/experiments")
def list_experiments(market: Market | None = None) -> list[dict]:
    return storage.list_experiments(market.value if market else None)


@v1.post("/experiments", status_code=201)
def create_experiment(req: ExperimentCreate) -> dict:
    experiment = Experiment(
        experiment_id=f"exp-{uuid4().hex[:12]}",
        created_at=_now(),
        **req.model_dump(),
    )
    storage.save_experiment(
        experiment.experiment_id, req.market.value,
        experiment.model_dump(mode="json"),
    )
    storage.audit("experiment.created", detail=experiment.experiment_id)
    return experiment.model_dump(mode="json")


@v1.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    experiment = storage.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


# ---- 候选 ----

@v1.get("/candidates")
def list_candidates(market: Market | None = None) -> list[dict]:
    items = (storage.get_candidate(cid) for cid in _candidate_ids(market))
    return [_public(c) for c in items if c]


def _candidate_ids(market: Market | None) -> list[str]:
    with storage.locked_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT candidate_id FROM candidates WHERE market = ?",
                (market.value,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT candidate_id FROM candidates").fetchall()
    return [r[0] for r in rows]


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    out["version"] = row.get("_version", 1)
    return out


@v1.post("/candidates", status_code=201)
def create_candidate(req: CandidateCreate) -> dict:
    candidate = StrategyCandidate(
        candidate_id=f"cand-{uuid4().hex[:12]}",
        updated_at=_now(),
        **req.model_dump(),
    )
    storage.save_candidate(
        candidate.candidate_id, req.market.value,
        candidate.stage.value, candidate.model_dump(mode="json"),
    )
    storage.audit("candidate.created", candidate_id=candidate.candidate_id,
                  to_stage=candidate.stage.value)
    return candidate.model_dump(mode="json")


@v1.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str) -> dict:
    row = storage.get_candidate(candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return _public(row)


@v1.post("/candidates/{candidate_id}/promote")
def promote_candidate(candidate_id: str, req: PromotionRequest) -> dict:
    row = storage.get_candidate(candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    # payload 里的 evidence_hash 是账本附属字段，非契约字段
    candidate = StrategyCandidate.model_validate(
        {k: v for k, v in row.items()
         if not k.startswith("_") and k != "evidence_hash"})
    current_version = row["_version"]

    # 1. 幂等晋级：同目标阶段 + 同证据哈希 + 同审批 → 返回当前状态
    ledger_refs = storage.evidence_refs(candidate_id)
    new_hash = storage.evidence_hash(ledger_refs + req.evidence_refs)
    last = _last_promotion(candidate_id)
    if (last and last["to_stage"] == req.target_stage.value
            and last["evidence_hash"] == new_hash
            and last.get("approval_id") == req.approval_id):
        return _public(storage.get_candidate(candidate_id))

    # 2. 证据防篡改：候选 payload 的 evidence_refs 必须与账本完全一致
    if sorted(candidate.evidence_refs) != sorted(ledger_refs):
        raise HTTPException(
            status_code=422,
            detail=("evidence tampering detected: candidate payload does "
                    "not match append-only evidence ledger; fail-closed"),
        )

    # 3. 人工审批回查：approval_id 必须在网关账本中真实有效且绑定本候选
    if req.target_stage in _STAGES_REQUIRING_AUDITOR:
        if not req.approval_id:
            raise HTTPException(
                status_code=422,
                detail=f"human approval_id is required for {req.target_stage.value}",
            )
        _verify_approval_with_gateway(candidate, req)

    # 4. 独立风控审计（失败关闭：不可达/超时/驳回均拒绝晋级）
    auditor_ref = None
    if req.target_stage in _STAGES_REQUIRING_AUDITOR:
        verdict = _audit_with_risk_auditor(
            candidate, req, new_hash)
        auditor_ref = verdict["conclusion_id"]

    # 5. 状态机门禁（证据数、审批 ID、单步推进）
    try:
        updated = promote(
            candidate, req.target_stage, req.evidence_refs, req.approval_id
        )
    except PromotionError as exc:
        storage.audit("promotion.rejected", candidate_id=candidate_id,
                      from_stage=candidate.stage.value,
                      to_stage=req.target_stage.value,
                      evidence_hash=new_hash, approval_id=req.approval_id,
                      detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 6. 乐观锁落库 + 证据与审计 append
    # promote() 已把新证据并入 updated.evidence_refs；哈希另存 payload
    if req.expected_version is not None and req.expected_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(f"version conflict: expected {req.expected_version},"
                    f" current {current_version}; retry with fresh version"),
        )
    if not storage.update_candidate_stage(
            candidate_id, updated.stage.value,
            {**updated.model_dump(mode="json"), "evidence_hash": new_hash},
            expected_version=current_version):
        raise HTTPException(
            status_code=409,
            detail=(f"concurrent promotion won (version {current_version});"
                    " no double transition applied"),
        )
    storage.append_evidence(candidate_id, req.evidence_refs)
    storage.audit(
        "promotion.applied", candidate_id=candidate_id,
        from_stage=candidate.stage.value, to_stage=updated.stage.value,
        evidence_hash=new_hash, approval_id=req.approval_id,
        detail=f"auditor={auditor_ref}" if auditor_ref else None,
    )
    result = storage.get_candidate(candidate_id)
    return _public(result)


def _last_promotion(candidate_id: str) -> dict | None:
    history = [h for h in storage.audit_history(candidate_id)
               if h["action"] == "promotion.applied"]
    return history[-1] if history else None


def _audit_with_risk_auditor(candidate: StrategyCandidate,
                             req: PromotionRequest,
                             ev_hash: str) -> dict:
    """调用独立 Risk Auditor。任何失败都失败关闭，绝不猜测放行。"""
    auditor_url = _auditor_url()
    if not auditor_url:
        raise HTTPException(
            status_code=503,
            detail=("risk auditor not configured"
                    " (STRATEGY_EVOLUTION_AUDITOR_URL); fail-closed"),
        )
    payload = {
        "candidate_id": candidate.candidate_id,
        "market": candidate.market.value,
        "strategy_id": candidate.strategy_id,
        "strategy_version": candidate.strategy_version,
        "from_stage": candidate.stage.value,
        "to_stage": req.target_stage.value,
        "evidence_refs": candidate.evidence_refs + req.evidence_refs,
        "evidence_hash": ev_hash,
        "approval_id": req.approval_id,
    }
    headers = None
    auditor_key = os.environ.get("RISK_AUDITOR_API_KEY")
    if auditor_key:
        headers = {"X-API-Key": auditor_key}
    try:
        resp = httpx.post(
            auditor_url.rstrip("/") + "/v1/audit-promotion",
            json=payload, headers=headers, timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"risk auditor unreachable; fail-closed: {exc}",
        ) from exc
    if resp.status_code >= 500:
        # Auditor 侧故障（含经代理转写的连接失败）：失败关闭，可重试
        raise HTTPException(
            status_code=503,
            detail=(f"risk auditor unavailable (http {resp.status_code}); "
                    f"fail-closed"),
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=422,
            detail=(f"risk auditor rejected promotion "
                    f"(http {resp.status_code}): {resp.text[:200]}"),
        )
    verdict = resp.json()
    if verdict.get("verdict") != "PASS":
        raise HTTPException(
            status_code=422,
            detail=(f"risk auditor verdict {verdict.get('verdict')}: "
                    f"{verdict.get('reason')}"),
        )
    return verdict


@v1.get("/candidates/{candidate_id}/audit-history")
def candidate_audit_history(candidate_id: str) -> list[dict]:
    if storage.get_candidate(candidate_id) is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return storage.audit_history(candidate_id)


app.include_router(v1)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "strategy-evolution"}

# Prometheus 指标：infra/observability/prometheus.yml 抓取 /metrics
from prometheus_client import make_asgi_app  # noqa: E402

app.mount("/metrics", make_asgi_app())
