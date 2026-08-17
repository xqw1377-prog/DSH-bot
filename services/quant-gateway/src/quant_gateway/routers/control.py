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


@router.post("/markets/{market}/kill-switch/resume")
def resume_kill_switch(
    market: Market,
    account_id: str | None = None,
    actor_id: str | None = None,
    principal: Principal = Depends(require_write),
):
    """Kill Switch 人工恢复。使用独立事件，不再借用 strategy.resumed。"""
    actor = actor_id or principal.name
    adapter = get_adapter(market)
    adapter.resume_trading()
    adapter.resume_strategy(account_id or "*")
    audit.record(
        "kill_switch.resumed", actor, market.value, account_id,
        detail="manual kill_switch resume",
    )
    return {
        "market": market,
        "account_id": account_id,
        "actor_id": actor,
        "status": "RESUMED",
    }


@router.post("/markets/{market}/emergency-stop")
def emergency_stop(
    market: Market,
    account_id: str | None = None,
    actor_id: str | None = None,
    principal: Principal = Depends(require_write),
):
    actor = actor_id or principal.name
    audit.record(
        "kill_switch.requested", actor, market.value, account_id,
        detail="manual emergency_stop",
    )
    try:
        get_adapter(market).emergency_stop(account_id=account_id)
    except Exception as exc:
        audit.record(
            "kill_switch.failed", actor, market.value, account_id, detail=str(exc),
        )
        raise
    audit.record(
        "kill_switch.succeeded", actor, market.value, account_id,
        detail="manual emergency_stop",
    )
    audit.record("emergency.stop", actor, market.value, account_id)
    return {
        "market": market,
        "account_id": account_id,
        "actor_id": actor,
        "status": "STOPPED",
    }
