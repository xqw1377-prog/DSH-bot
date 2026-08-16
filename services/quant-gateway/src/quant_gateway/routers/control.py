"""Quant Gateway 控制接口（PRD 11.1）。

pause/resume 与 emergency_stop 会改变策略/账户状态，
必须走审批并在审计中记录。
"""

from fastapi import APIRouter

from dsh_contracts import Market
from quant_gateway.adapters import get_adapter

router = APIRouter()


@router.post("/markets/{market}/strategies/{strategy_id}/pause")
def pause_strategy(market: Market, strategy_id: str):
    get_adapter(market).pause_strategy(strategy_id)
    return {"strategy_id": strategy_id, "status": "PAUSED"}


@router.post("/markets/{market}/strategies/{strategy_id}/resume")
def resume_strategy(market: Market, strategy_id: str):
    get_adapter(market).resume_strategy(strategy_id)
    return {"strategy_id": strategy_id, "status": "RESUMED"}


@router.post("/markets/{market}/emergency-stop")
def emergency_stop(market: Market, account_id: str | None = None):
    get_adapter(market).emergency_stop(account_id=account_id)
    return {"market": market, "account_id": account_id, "status": "STOPPED"}
