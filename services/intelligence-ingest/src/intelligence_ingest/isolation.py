"""情报服务隔离：禁止交易密钥、Gateway 写权限、浏览器硬爬 X。"""

from __future__ import annotations

import os

FORBIDDEN_ENV = (
    "BINANCE_API_SECRET",
    "BINANCE_SECRET_KEY",
    "OKX_SECRET_KEY",
    "OKX_API_SECRET",
    "BYBIT_API_SECRET",
    "ZISU_DB",
    "BROKER_SECRET",
    "EXCHANGE_API_SECRET",
)

FORBIDDEN_METHODS = frozenset({"PLAYWRIGHT", "SELENIUM", "X_BROWSER"})


class IsolationError(RuntimeError):
    pass


def assert_isolated(environ: dict[str, str] | None = None) -> None:
    env = environ if environ is not None else os.environ
    for name in FORBIDDEN_ENV:
        if str(env.get(name) or "").strip():
            raise IsolationError(f"intelligence-ingest must not load {name}")
    keys = str(env.get("QUANT_GATEWAY_API_KEYS") or "")
    if ":write" in keys or "/write" in keys:
        raise IsolationError("intelligence-ingest must not hold a Gateway write key")
    if str(env.get("DSH_INTEL_ALLOW_PLAYWRIGHT") or "").strip() in {"1", "true", "TRUE"}:
        raise IsolationError("Playwright is last-resort and disabled for this service")
    if str(env.get("DSH_INTEL_ALLOW_X_BROWSER") or "").strip() in {"1", "true", "TRUE"}:
        raise IsolationError("X browser scrape is forbidden; use official Filtered Stream")
