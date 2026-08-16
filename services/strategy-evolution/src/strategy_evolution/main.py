"""策略持续进化服务。

实验账本与策略晋级状态机。不能直接修改生产策略：
生产策略变更必须由量化系统在本服务输出晋级结论并经人工审批后执行。
当前使用内存存储，生产应替换为持久化账本。
"""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import Experiment, Market, StrategyCandidate, StrategyStage
from strategy_evolution.state_machine import PromotionError, promote

app = FastAPI(
    title="Strategy Evolution",
    description=(
        "策略持续进化服务。负责实验账本、验证门禁、策略晋级状态机。"
        "不能直接修改生产策略。"
    ),
    version="0.2.0",
)

# 内存账本：进程重启即失，仅用于本地开发与联调。
_experiments: dict[str, Experiment] = {}
_candidates: dict[str, StrategyCandidate] = {}


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


v1 = APIRouter(prefix="/v1")


@v1.get("/experiments")
def list_experiments(market: Market | None = None) -> list[dict]:
    items = _experiments.values()
    if market is not None:
        items = (e for e in items if e.market == market)
    return [e.model_dump(mode="json") for e in items]


@v1.post("/experiments", status_code=201)
def create_experiment(req: ExperimentCreate) -> dict:
    experiment = Experiment(
        experiment_id=f"exp-{uuid4().hex[:12]}",
        created_at=_now(),
        **req.model_dump(),
    )
    _experiments[experiment.experiment_id] = experiment
    return experiment.model_dump(mode="json")


@v1.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    experiment = _experiments.get(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment.model_dump(mode="json")


@v1.get("/candidates")
def list_candidates(market: Market | None = None) -> list[dict]:
    items = _candidates.values()
    if market is not None:
        items = (c for c in items if c.market == market)
    return [c.model_dump(mode="json") for c in items]


@v1.post("/candidates", status_code=201)
def create_candidate(req: CandidateCreate) -> dict:
    candidate = StrategyCandidate(
        candidate_id=f"cand-{uuid4().hex[:12]}",
        updated_at=_now(),
        **req.model_dump(),
    )
    _candidates[candidate.candidate_id] = candidate
    return candidate.model_dump(mode="json")


@v1.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str) -> dict:
    candidate = _candidates.get(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return candidate.model_dump(mode="json")


@v1.post("/candidates/{candidate_id}/promote")
def promote_candidate(candidate_id: str, req: PromotionRequest) -> dict:
    candidate = _candidates.get(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        updated = promote(
            candidate, req.target_stage, req.evidence_refs, req.approval_id
        )
    except PromotionError as exc:
        # 门禁不满足 = 422，不是服务器错误
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _candidates[candidate_id] = updated
    return updated.model_dump(mode="json")


app.include_router(v1)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "strategy-evolution"}
