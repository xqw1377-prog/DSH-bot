import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quant_gateway import audit
from quant_gateway.auth import enforce_startup_auth
from quant_gateway.routers import approvals, control, orders, read_only


@asynccontextmanager
async def lifespan(_app: FastAPI):
    enforce_startup_auth()
    if os.environ.get("DSH_LOCAL_PAPER") == "1":
        from quant_gateway.adapters.paper import register_paper_adapters

        register_paper_adapters()
    yield


app = FastAPI(
    title="Quant Gateway",
    description=(
        "DSH Bot 与现有量化系统之间的稳定边界。"
        "DSH 插件不得直接访问交易数据库或券商/交易所密钥。"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(read_only.router, prefix="/v1", tags=["read-only"])
app.include_router(orders.router, prefix="/v1", tags=["orders"])
app.include_router(approvals.router, prefix="/v1", tags=["approvals"])
app.include_router(control.router, prefix="/v1", tags=["control"])
app.include_router(audit.router, prefix="/v1", tags=["audit"])


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "quant-gateway"}
