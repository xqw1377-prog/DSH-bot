"""情报服务。只读查询 + 手动触发一次采集。无交易接口。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from intelligence_ingest.isolation import IsolationError, assert_isolated
from intelligence_ingest.pipeline import ingest_once
from intelligence_ingest.registry import load_registry, x_filter_rules
from intelligence_ingest.store import IntelligenceStore

app = FastAPI(
    title="DSH Intelligence Ingest",
    description="官方 API/RSS/增量 HTML。事件只进 Shadow。无交易密钥。",
    version="0.1.0",
)


@app.on_event("startup")
def _startup() -> None:
    assert_isolated()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "intelligence-ingest", "mode": "SHADOW"}


@app.get("/v1/sources")
def list_sources() -> dict:
    registry = load_registry()
    return {
        "us_market": registry.us_market,
        "crypto_assets": [item.__dict__ for item in registry.crypto_assets],
        "sources": [item.__dict__ for item in registry.sources],
        "x_filter_rules": x_filter_rules(registry),
        "enabled": [item.id for item in registry.enabled_sources()],
    }


@app.get("/v1/documents")
def list_documents(limit: int = 50) -> list[dict]:
    return IntelligenceStore().recent_documents(limit)


@app.get("/v1/events")
def list_events(limit: int = 50) -> list[dict]:
    return IntelligenceStore().recent_events(limit)


@app.get("/v1/source-health")
def source_health() -> list[dict]:
    """源健康状态:连续失败、最近成功/失败时间、恢复时间。

    连续失败 > 0 的源即「中断」,恢复时间非空表示已补采恢复
    (RSS/Atom 拉取最近条目,成功的一拉即覆盖中断窗口)。
    """
    return IntelligenceStore().list_source_health()


@app.post("/v1/ingest")
def run_ingest(include_derived: bool = False) -> dict:
    try:
        return ingest_once(include_derived=include_derived)
    except IsolationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
