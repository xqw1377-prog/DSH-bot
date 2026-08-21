"""Quant Gateway 控制接口（PRD 11.1）。

pause/resume 与 emergency_stop 会改变策略/账户状态，
必须走审批并在审计中记录。
"""

from fastapi import APIRouter, Depends

from dsh_contracts import Market
from quant_gateway import audit
from quant_gateway.adapters import get_adapter
from quant_gateway.auth import (
    Principal,
    require_actor_principal,
    require_bff_service,
    require_write,
)

router = APIRouter(dependencies=[Depends(require_write)])


@router.post("/markets/{market}/strategies/{strategy_id}/pause")
def pause_strategy(market: Market, strategy_id: str,
                   principal: Principal = Depends(require_write),
                   actor_principal: str = Depends(require_actor_principal)):
    require_bff_service(principal)
    get_adapter(market).pause_strategy(strategy_id)
    audit.record(
        "strategy.paused",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=strategy_id,
    )
    return {"strategy_id": strategy_id, "status": "PAUSED"}


@router.post("/markets/{market}/strategies/{strategy_id}/resume")
def resume_strategy(market: Market, strategy_id: str,
                    principal: Principal = Depends(require_write),
                    actor_principal: str = Depends(require_actor_principal)):
    require_bff_service(principal)
    get_adapter(market).resume_strategy(strategy_id)
    audit.record(
        "strategy.resumed",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=strategy_id,
    )
    return {"strategy_id": strategy_id, "status": "RESUMED"}


@router.post("/markets/{market}/kill-switch/resume")
def resume_kill_switch(
    market: Market,
    account_id: str | None = None,
    principal: Principal = Depends(require_write),
    actor_principal: str = Depends(require_actor_principal),
):
    """Kill Switch 人工恢复。使用独立事件，不再借用 strategy.resumed。

    恢复语义:只解除交易通道停机(resume_trading),不越权恢复具体策略
    ——账户 ID 不是策略 ID,"*" 恢复全部策略更越权。策略恢复走
    /strategies/{id}/resume,那是独立的、需另行授权的动作。
    """
    require_bff_service(principal)
    adapter = get_adapter(market)
    adapter.resume_trading()
    audit.record(
        "kill_switch.resumed",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=account_id,
        detail="manual kill_switch resume",
    )
    return {
        "market": market,
        "account_id": account_id,
        "actor_principal": actor_principal,
        "status": "RESUMED",
    }



@router.post("/markets/{market}/emergency-stop")
def emergency_stop(
    market: Market,
    account_id: str | None = None,
    principal: Principal = Depends(require_write),
    actor_principal: str = Depends(require_actor_principal),
):
    require_bff_service(principal)
    audit.record(
        "kill_switch.requested",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=account_id,
        detail="manual emergency_stop",
    )
    try:
        get_adapter(market).emergency_stop(account_id=account_id)
    except Exception as exc:
        audit.record(
            "kill_switch.failed",
            service_principal=principal.name,
            actor_principal=actor_principal,
            market=market.value,
            subject_id=account_id,
            detail=str(exc),
        )
        raise
    audit.record(
        "kill_switch.succeeded",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=account_id,
        detail="manual emergency_stop",
    )
    audit.record(
        "emergency.stop",
        service_principal=principal.name,
        actor_principal=actor_principal,
        market=market.value,
        subject_id=account_id,
    )
    return {
        "market": market,
        "account_id": account_id,
        "actor_principal": actor_principal,
        "status": "STOPPED",
    }
