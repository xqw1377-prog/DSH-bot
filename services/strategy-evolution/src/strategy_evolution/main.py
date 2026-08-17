"""策略持续进化服务。

实验账本与策略晋级状态机。不能直接修改生产策略：
生产策略变更必须由量化系统在本服务输出晋级结论并经人工审批后执行。

持久化：实验与候选存 SQLite（STRATEGY_EVOLUTION_DB），重启不丢失。
门禁执行器：晋级到 APPROVED/CANARY/PRODUCTION 时，必须调用独立 Risk Auditor
HTTP 服务（STRATEGY_EVOLUTION_AUDITOR_URL）；未配置或不可达时失败关闭。
"""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import Experiment, Market, StrategyCandidate, StrategyStage, TaskStatus
from strategy_evolution import storage
from strategy_evolution.state_machine import PromotionError, promote


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.get_conn()
    yield


app = FastAPI(
    title="Strategy Evolution",
    description=(
        "策略持续进化服务。负责实验账本、验证门禁、策略晋级状态机。"
        "不能直接修改生产策略。"
    ),
    version="0.3.0",
    lifespan=lifespan,
)

AUDITOR_URL = os.environ.get("STRATEGY_EVOLUTION_AUDITOR_URL", "")
# 进入这些阶段前必须通过独立审计（与 risk-auditor 二次校验红线对齐）
_AUDIT_REQUIRED_STAGES = {
    StrategyStage.APPROVED, StrategyStage.CANARY, StrategyStage.PRODUCTION,
}


def _now() -> datetime:
    return datetime.now(UTC)


class ExperimentCreate(BaseModel):
    market: Market
    strategy_id: str
    hypothesis: str
    data_snapshot_id: str
    created_by_bot: str = "market-chief"


class ExperimentResultUpdate(BaseModel):
    result_ref: str


class CandidateCreate(BaseModel):
    market: Market
    strategy_id: str
    strategy_version: str


class PromotionRequest(BaseModel):
    target_stage: StrategyStage
    evidence_refs: list[str] = Field(default_factory=list)
    approval_id: str | None = None


v1 = APIRouter(prefix="/v1")


# ---- 实验账本 ----

@v1.get("/experiments")
def list_experiments(market: Market | None = None) -> list[dict]:
    sql = "SELECT payload FROM experiments"
    params: list = []
    if market is not None:
        sql += " WHERE market = ?"
        params.append(market.value)
    rows = storage.get_conn().execute(sql, params).fetchall()
    import json
    return [json.loads(r[0]) for r in rows]


@v1.post("/experiments", status_code=201)
def create_experiment(req: ExperimentCreate) -> dict:
    experiment = Experiment(
        experiment_id=f"exp-{uuid4().hex[:12]}",
        created_at=_now(),
        **req.model_dump(),
    )
    _save_experiment(experiment)
    return experiment.model_dump(mode="json")


@v1.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    experiment = _get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment.model_dump(mode="json")


@v1.post("/experiments/{experiment_id}/result")
def record_result(experiment_id: str, req: ExperimentResultUpdate) -> dict:
    experiment = _get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    updated = experiment.model_copy(update={
        "result_ref": req.result_ref,
        "status": TaskStatus.COMPLETED,
    })
    _save_experiment(updated)
    return updated.model_dump(mode="json")


def _get_experiment(experiment_id: str) -> Experiment | None:
    row = storage.get_conn().execute(
        "SELECT payload FROM experiments WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    return Experiment.model_validate_json(row[0])


def _save_experiment(experiment: Experiment) -> None:
    conn = storage.get_conn()
    conn.execute(
        """INSERT INTO experiments
           (experiment_id, market, strategy_id, hypothesis, data_snapshot_id,
            status, created_by_bot, created_at, result_ref, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(experiment_id) DO UPDATE SET
               status = excluded.status,
               result_ref = excluded.result_ref,
               payload = excluded.payload""",
        (
            experiment.experiment_id, experiment.market.value,
            experiment.strategy_id, experiment.hypothesis,
            experiment.data_snapshot_id, experiment.status.value,
            experiment.created_by_bot, experiment.created_at.isoformat(),
            experiment.result_ref, experiment.model_dump_json(),
        ),
    )
    conn.commit()


# ---- 策略候选 ----

@v1.get("/candidates")
def list_candidates(market: Market | None = None) -> list[dict]:
    sql = "SELECT payload FROM candidates"
    params: list = []
    if market is not None:
        sql += " WHERE market = ?"
        params.append(market.value)
    rows = storage.get_conn().execute(sql, params).fetchall()
    import json
    return [json.loads(r[0]) for r in rows]


@v1.post("/candidates", status_code=201)
def create_candidate(req: CandidateCreate) -> dict:
    candidate = StrategyCandidate(
        candidate_id=f"cand-{uuid4().hex[:12]}",
        updated_at=_now(),
        **req.model_dump(),
    )
    _save_candidate(candidate)
    return candidate.model_dump(mode="json")


@v1.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str) -> dict:
    candidate = _get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return candidate.model_dump(mode="json")


@v1.post("/candidates/{candidate_id}/promote")
def promote_candidate(candidate_id: str, req: PromotionRequest) -> dict:
    candidate = _get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        updated = promote(
            candidate, req.target_stage, req.evidence_refs, req.approval_id
        )
    except PromotionError as exc:
        # 门禁不满足 = 422，不是服务器错误
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 独立审计门禁：APPROVED/CANARY/PRODUCTION 必须经 risk-auditor HTTP 二次校验。
    # 未配置 auditor URL 或服务不可达 = 失败关闭（禁止跳过）。
    if req.target_stage in _AUDIT_REQUIRED_STAGES:
        if not AUDITOR_URL:
            raise HTTPException(
                status_code=503,
                detail=(
                    "STRATEGY_EVOLUTION_AUDITOR_URL is not configured; "
                    "fail-closed, promotion rejected"
                ),
            )
        audit = _run_independent_audit(updated)
        if audit is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "independent auditor unreachable; "
                    "fail-closed, promotion rejected"
                ),
            )
        if not audit.get("approved"):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"independent audit rejected promotion to {req.target_stage}; "
                    f"audit_id={audit.get('audit_id')} reason={audit.get('reason')}"
                ),
            )
        # 将审计证据回写到候选，便于重启后追溯
        merged_refs = list(updated.evidence_refs)
        audit_ref = f"audit:{audit.get('audit_id')}:{audit.get('evidence_hash')}"
        if audit_ref not in merged_refs:
            merged_refs.append(audit_ref)
        updated = updated.model_copy(update={"evidence_refs": merged_refs})

    _save_candidate(updated)
    return updated.model_dump(mode="json")


def _get_candidate(candidate_id: str) -> StrategyCandidate | None:
    row = storage.get_conn().execute(
        "SELECT payload FROM candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    return StrategyCandidate.model_validate_json(row[0])


def _save_candidate(candidate: StrategyCandidate) -> None:
    conn = storage.get_conn()
    conn.execute(
        """INSERT INTO candidates
           (candidate_id, market, strategy_id, strategy_version, stage,
            experiment_id, evidence_refs, approval_id, updated_at, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(candidate_id) DO UPDATE SET
               stage = excluded.stage,
               evidence_refs = excluded.evidence_refs,
               approval_id = excluded.approval_id,
               updated_at = excluded.updated_at,
               payload = excluded.payload""",
        (
            candidate.candidate_id, candidate.market.value,
            candidate.strategy_id, candidate.strategy_version,
            candidate.stage.value, candidate.experiment_id,
            "[]",  # evidence_refs 作为 payload 的一部分，单独列仅索引用
            candidate.approval_id, candidate.updated_at.isoformat(),
            candidate.model_dump_json(),
        ),
    )
    conn.commit()


def _run_independent_audit(candidate: StrategyCandidate) -> dict | None:
    """调用独立 Risk Auditor HTTP 服务。

    返回审计结果 dict；不可达返回 None（由调用方失败关闭）。
    """
    try:
        resp = httpx.post(
            AUDITOR_URL.rstrip("/") + "/v1/audit-promotion",
            json={
                "candidate": candidate.model_dump(mode="json"),
                "evidence_refs": candidate.evidence_refs,
                "upstream_passed": True,
            },
            timeout=3.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "approved" not in data:
            return None
        return data
    except (httpx.HTTPError, ValueError, TypeError):
        return None


app.include_router(v1)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "strategy-evolution"}
