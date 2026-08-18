"""Projection 服务身份。只保护敏感只读聚合，不改变 Gateway 资金路径。"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _keys() -> set[str]:
    raw = os.environ.get("PROJECTION_API_KEYS") or os.environ.get("PROJECTION_API_KEY") or ""
    return {item.strip() for item in raw.split(";") if item.strip()}


def auth_enabled() -> bool:
    return bool(_keys())


def require_projection_read(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    env = os.environ.get("DSH_ENV", "production")
    keys = _keys()
    if env == "development" and not keys:
        return
    if not keys:
        raise HTTPException(
            status_code=503,
            detail="projection fail-closed: PROJECTION_API_KEY required",
        )
    if not x_api_key or x_api_key not in keys:
        raise HTTPException(status_code=401, detail="missing or invalid projection service identity")
