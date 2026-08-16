from fastapi import FastAPI

from quant_gateway.routers import control, orders, read_only

app = FastAPI(
    title="Quant Gateway",
    description=(
        "DSH Bot 与现有量化系统之间的稳定边界。"
        "DSH 插件不得直接访问交易数据库或券商/交易所密钥。"
    ),
    version="0.1.0",
)

app.include_router(read_only.router, prefix="/v1", tags=["read-only"])
app.include_router(orders.router, prefix="/v1", tags=["orders"])
app.include_router(control.router, prefix="/v1", tags=["control"])


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "quant-gateway"}
