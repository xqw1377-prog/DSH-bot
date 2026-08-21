"""策略进化服务身份。

与 projection-api 的鉴权风格一致:
- <KEYS_ENV>(分号分隔)配置后强制 X-API-Key
- 开发模式(DSH_ENV=development)未配置时放行,便于本地联调
- 生产模式未配置 = 失败关闭(503):不允许匿名访问内部服务
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _keys() -> set[str]:
    raw = os.environ.get("STRATEGY_EVOLUTION_API_KEYS") or ""
    return {item.strip() for item in raw.split(";") if item.strip()}


def auth_enabled() -> bool:
    return bool(_keys())


def require_service_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    env = os.environ.get("DSH_ENV", "production")
    keys = _keys()
    if env == "development" and not keys:
        return
    if not keys:
        raise HTTPException(
            status_code=503,
            detail="strategy-evolution fail-closed: STRATEGY_EVOLUTION_API_KEYS required",
        )
    if not x_api_key or x_api_key not in keys:
        raise HTTPException(
            status_code=401,
            detail="missing or invalid strategy-evolution service identity",
        )
