import os

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Projection API",
    description="面向前端的只读投影。不处理资金动作。",
    version="0.2.0",
)

QUANT_GATEWAY_URL = os.environ.get("QUANT_GATEWAY_URL", "http://127.0.0.1:8001")
STRATEGY_EVOLUTION_URL = os.environ.get(
    "STRATEGY_EVOLUTION_URL", "http://127.0.0.1:8002"
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "projection-api"}


async def _proxy_get(base_url: str, path: str, params: dict | None = None):
    """透传上游只读响应。

    上游状态码必须原样传递：503 表示失败关闭，不能被压成 500，
    否则前端无法区分「网关拒绝」和「投影服务自身故障」。
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base_url}{path}", params=params)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"upstream unreachable: {exc}")
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def _proxy_gateway(path: str, params: dict | None = None):
    return await _proxy_get(QUANT_GATEWAY_URL, path, params)


async def _proxy_evolution(path: str, params: dict | None = None):
    return await _proxy_get(STRATEGY_EVOLUTION_URL, path, params)


@app.get("/v1/markets/{market}/health")
async def get_health(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/health")


@app.get("/v1/markets/{market}/positions")
async def get_positions(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/positions")


@app.get("/v1/markets/{market}/accounts")
async def get_accounts(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/accounts")


@app.get("/v1/markets/{market}/signals")
async def get_signals(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/signals")


@app.get("/v1/approvals")
async def get_approvals(status: str | None = None, market: str | None = None):
    params = {k: v for k, v in {"status": status, "market": market}.items() if v}
    return await _proxy_gateway("/v1/approvals", params=params or None)


@app.get("/v1/experiments")
async def get_experiments(market: str | None = None):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/experiments", params=params)


@app.get("/v1/candidates")
async def get_candidates(market: str | None = None):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/candidates", params=params)


@app.get("/v1/markets/{market}/orders/{order_id}")
async def get_order(market: str, order_id: str):
    return await _proxy_gateway(f"/v1/markets/{market}/orders/{order_id}")


@app.get("/v1/bot-tasks")
def get_bot_tasks(bot: str | None = None, status: str | None = None):
    """只读投影 Runtime 任务，供异常路径与对账状态展示。"""
    import json
    import sqlite3

    db = os.environ.get("DSH_RUNTIME_DB", "")
    if not db or db.startswith(":memory"):
        return []
    if not os.path.isfile(db):
        return []
    conn = sqlite3.connect(db)
    sql = (
        "SELECT task_id, bot, kind, status, subject_id, approval_id, order_id,"
        " COALESCE(reconciliation_status, 'PENDING'), payload, updated_at"
        " FROM bot_tasks"
    )
    params: list = []
    clauses = []
    if bot:
        clauses.append("bot = ?")
        params.append(bot)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "task_id": r[0],
            "bot": r[1],
            "kind": r[2],
            "status": r[3],
            "subject_id": r[4],
            "approval_id": r[5],
            "order_id": r[6],
            "reconciliation_status": r[7],
            "payload": json.loads(r[8]) if r[8] else {},
            "updated_at": r[9],
        }
        for r in rows
    ]
