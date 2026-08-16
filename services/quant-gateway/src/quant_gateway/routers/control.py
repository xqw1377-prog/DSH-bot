"""Quant Gateway 控制接口（PRD 11.1）。

pause/resume 与 emergency_stop 会改变策略/账户状态，
必须走审批并在审计中记录。
"""

from fastapi import APIRouter, Depends

from dsh_contracts import Market
from quant_gateway import audit
from quant_gateway.adapters import get_adapter
from quant_gateway.auth import Principal, require_write

router = APIRouter(dependencies=[Depends(require_write)])


@router.post("/markets/{market}/strategies/{strategy_id}/pause")
def pause_strategy(market: Market, strategy_id: str,
                   principal: Principal = Depends(require_write)):
    get_adapter(market).pause_strategy(strategy_id)
    audit.record("strategy.paused", principal.name, market.value, strategy_id)
    return {"strategy_id": strategy_id, "status": "PAUSED"}


@router.post("/markets/{market}/strategies/{strategy_id}/resume")
def resume_strategy(market: Market, strategy_id: str,
                    principal: Principal = Depends(require_write)):
    get_adapter(market).resume_strategy(strategy_id)
    audit.record("strategy.resumed", principal.name, market.value, strategy_id)
    return {"strategy_id": strategy_id, "status": "RESUMED"}


@router.post("/markets/{market}/emergency-stop")
def emergency_stop(market: Market, account_id: str | None = None,
                   principal: Principal = Depends(require_write)):
    get_adapter(market).emergency_stop(account_id=account_id)
    audit.record("emergency.stop", principal.name, market.value, account_id)
    return {"market": market, "account_id": account_id, "status": "STOPPED"}
