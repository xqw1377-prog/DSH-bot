"""Quant Gateway API Key 鉴权。

配置（环境变量 QUANT_GATEWAY_API_KEYS），分号分隔，每项 `key:scope1,scope2`：
    QUANT_GATEWAY_API_KEYS="chief-key:read,write;audit-key:read"

- read：只读接口（行情、持仓、账户、信号、订单状态、审批列表）
- write：资金/状态变更接口（下单、撤单、审批决定、策略控制、审批创建）

未配置任何 key 时为开发开放模式（全部放行），便于本地联调；
生产必须配置。鉴权开启时审批 decided_by 由 Gateway 覆盖为 principal.name，
客户端提交的 decided_by 无效（见 routers/approvals.py）。
"""

import os
from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class Principal:
    api_key: str
    name: str
    scopes: frozenset[str]


def _load_principals() -> dict[str, Principal]:
    principals: dict[str, Principal] = {}
    raw = os.environ.get("QUANT_GATEWAY_API_KEYS", "")
    for entry in filter(None, (e.strip() for e in raw.split(";"))):
        try:
            key_part, scope_part = entry.split(":", 1)
            key, name = (
                key_part.split("/", 1) if "/" in key_part else (key_part, key_part)
            )
            scopes = frozenset(
                s.strip() for s in scope_part.split(",") if s.strip()
            )
        except ValueError:
            continue
        principals[key] = Principal(api_key=key, name=name, scopes=scopes)
    return principals


def auth_enabled() -> bool:
    return bool(_load_principals())


def enforce_startup_auth() -> None:
    """启动检查：非开发环境必须配置 API key，否则拒绝启动。

    开放模式（无鉴权）只能由 DSH_ENV=development 显式启用，
    默认（未设置或 production）一律失败关闭。
    """
    env = os.environ.get("DSH_ENV", "production")
    if env == "development":
        return
    if not auth_enabled():
        raise RuntimeError(
            "refusing to start: QUANT_GATEWAY_API_KEYS is not configured "
            "and DSH_ENV is not 'development'; fail-closed"
        )


def _authenticate(x_api_key: str | None) -> Principal:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key header")
    principal = _load_principals().get(x_api_key)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return principal


def require_scope(scope: str):
    def dependency(x_api_key: str | None = Header(default=None)) -> Principal:
        if scope == "write" and os.environ.get("QUANT_GATEWAY_READ_ONLY") == "1":
            raise HTTPException(
                status_code=403,
                detail="gateway is read-only; write endpoints fail closed",
            )
        if not auth_enabled():
            return Principal(api_key="", name="anonymous", scopes=frozenset({"read", "write"}))
        principal = _authenticate(x_api_key)
        if scope not in principal.scopes:
            raise HTTPException(
                status_code=403,
                detail=f"api key '{principal.name}' lacks required scope '{scope}'",
            )
        return principal

    return dependency


require_read = require_scope("read")
require_write = require_scope("write")
