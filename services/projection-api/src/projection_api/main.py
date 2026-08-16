import os

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Projection API",
    description="面向前端的只读投影。不处理资金动作。",
    version="0.1.0",
)

QUANT_GATEWAY_URL = os.environ.get("QUANT_GATEWAY_URL", "http://127.0.0.1:8001")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "projection-api"}


async def _proxy_get(path: str):
    """透传 Quant Gateway 的只读响应。

    上游状态码必须原样传递：503 表示失败关闭，不能被压成 500，
    否则前端无法区分「网关拒绝」和「投影服务自身故障」。
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{QUANT_GATEWAY_URL}{path}")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"quant-gateway unreachable: {exc}")
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/v1/markets/{market}/health")
async def get_health(market: str):
    return await _proxy_get(f"/v1/markets/{market}/health")


@app.get("/v1/markets/{market}/positions")
async def get_positions(market: str):
    return await _proxy_get(f"/v1/markets/{market}/positions")


@app.get("/v1/markets/{market}/accounts")
async def get_accounts(market: str):
    return await _proxy_get(f"/v1/markets/{market}/accounts")


@app.get("/v1/markets/{market}/signals")
async def get_signals(market: str):
    return await _proxy_get(f"/v1/markets/{market}/signals")
