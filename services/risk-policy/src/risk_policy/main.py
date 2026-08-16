from fastapi import FastAPI

app = FastAPI(
    title="Risk Policy",
    description="全局风险预算、限制命中与二次风控策略。",
    version="0.1.0",
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "risk-policy"}


@app.get("/v1/risk-budget")
def get_risk_budget():
    return {
        "A_SHARE": {"max_position": "100000", "max_drawdown": "0.05"},
        "CRYPTO": {"max_position": "50000", "max_drawdown": "0.10"},
    }
