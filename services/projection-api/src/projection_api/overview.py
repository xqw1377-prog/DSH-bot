"""三 Bot 只读总览。不触发资金动作，不写 Gateway。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

FetchJson = Callable[[str], dict | list | None]

BOTS = (
    {
        "bot_id": "market-chief",
        "label": "Market Chief",
        "market": None,
        "runtime_bot": None,
        "read_only": True,
        "mode_env": None,
    },
    {
        "bot_id": "crypto",
        "label": "Crypto Bot",
        "market": "CRYPTO",
        "runtime_bot": "crypto-bot",
        "read_only": False,
        "mode_env": "DSH_CRYPTO_MODE",
    },
    {
        "bot_id": "a-share",
        "label": "A 股 Bot",
        "market": "A_SHARE",
        "runtime_bot": "a-stock-bot",
        "read_only": False,
        "mode_env": "DSH_A_SHARE_MODE",
    },
)

_ACTIVE_ORDER = {
    "APPROVED_SUBMITTING",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
}
_ANALYZING = {"SIGNAL_RECEIVED", "PREVIEWED", "RUNNING"}
_UNKNOWN = {
    "SUBMISSION_UNKNOWN",
    "APPROVAL_UNKNOWN",
    "UNKNOWN",
}
_REJECTED = {"ORDER_REJECTED", "REJECTED"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_mode(raw: str | None) -> str:
    value = (raw or "paper").strip().lower()
    if value == "shadow":
        return "SHADOW"
    if value == "live":
        return "LIVE"
    return "PAPER"


def global_mode(modes: list[str]) -> str:
    unique = {m for m in modes if m in {"PAPER", "SHADOW"}}
    if "LIVE" in modes:
        return "MIXED"
    if unique == {"PAPER"}:
        return "PAPER"
    if unique == {"SHADOW"}:
        return "SHADOW"
    if len(unique) > 1:
        return "MIXED"
    return "PAPER"


def default_gateway_fetch(path: str) -> dict | list | None:
    from projection_api.main import QUANT_GATEWAY_API_KEY, QUANT_GATEWAY_URL

    headers = (
        {"X-API-Key": QUANT_GATEWAY_API_KEY} if QUANT_GATEWAY_API_KEY else None
    )
    try:
        resp = httpx.get(
            f"{QUANT_GATEWAY_URL}{path}",
            headers=headers,
            timeout=2.0,
        )
    except Exception:
        return None
    if not resp.is_success:
        return None
    return resp.json()


def _health_runtime(health: dict | None) -> str:
    if health is None:
        return "OFFLINE"
    if health.get("degraded") or not health.get("system_ok") or not health.get(
        "trading_channel_ok", True
    ):
        return "DEGRADED"
    return "ONLINE"


def _health_data(health: dict | None) -> str:
    if health is None:
        return "DISCONNECTED"
    if not health.get("data_fresh"):
        return "STALE"
    return "FRESH"


def _task_dimension(tasks: list[dict]) -> str:
    statuses = {t.get("status") for t in tasks}
    if any(t.get("status") == "FILLED" for t in tasks):
        return "RECONCILING"
    if statuses & _ACTIVE_ORDER:
        return "EXECUTING"
    if "AWAITING_APPROVAL" in statuses:
        return "AWAITING_APPROVAL"
    if statuses & _ANALYZING:
        return "ANALYZING"
    return "IDLE"


def _order_dimension(tasks: list[dict]) -> str:
    statuses = {t.get("status") for t in tasks}
    recon = {t.get("reconciliation_status") for t in tasks}
    if statuses & _UNKNOWN or "UNKNOWN" in recon:
        return "UNKNOWN"
    if statuses & _REJECTED:
        return "REJECTED"
    if "PARTIALLY_FILLED" in statuses:
        return "PARTIAL"
    if statuses & _ACTIVE_ORDER:
        return "OPEN"
    return "NONE"


def _risk_dimension(
    health: dict | None,
    tasks: list[dict],
    incidents: list[dict],
    halted: bool,
) -> str:
    if halted:
        return "HALTED"
    if any(t.get("status") == "INCIDENT" for t in tasks):
        return "INCIDENT"
    open_incidents = [
        i
        for i in incidents
        if i.get("event_type") in ("incident/opened", "account/mismatch")
    ]
    if open_incidents:
        return "INCIDENT"
    if health is None:
        return "WARNING"
    if health.get("degraded") or not health.get("data_fresh"):
        return "WARNING"
    if any(t.get("reconciliation_status") == "MISMATCH" for t in tasks):
        return "WARNING"
    return "NORMAL"


def _kill_switch_halted(incidents: list[dict], market: str | None) -> bool:
    relevant = [
        i
        for i in incidents
        if (market is None or i.get("market") == market)
        and i.get("event_type") in ("kill_switch/succeeded", "kill_switch/resumed")
    ]
    relevant.sort(key=lambda row: row.get("occurred_at") or "", reverse=True)
    return bool(relevant) and relevant[0].get("event_type") == "kill_switch/succeeded"


def _filter_tasks(tasks: list[dict], runtime_bot: str | None) -> list[dict]:
    if runtime_bot is None:
        return tasks
    return [t for t in tasks if t.get("bot") == runtime_bot]


def _filter_incidents(incidents: list[dict], market: str | None) -> list[dict]:
    if market is None:
        return incidents
    return [i for i in incidents if i.get("market") == market]


def _approvals_count(rows: list | None, market: str | None) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(
        1
        for row in rows
        if row.get("status") == "REQUESTED"
        and (market is None or row.get("market") == market)
    )


def _build_bot(
    spec: dict,
    health: dict | None,
    tasks: list[dict],
    incidents: list[dict],
    approvals: list | None,
) -> dict[str, Any]:
    market = spec["market"]
    bot_tasks = _filter_tasks(tasks, spec["runtime_bot"])
    bot_incidents = _filter_incidents(incidents, market)
    halted = False
    if health is not None and (
        not health.get("trading_channel_ok", True)
        or "emergency stop" in (health.get("detail") or "").lower()
    ):
        halted = True
    if _kill_switch_halted(bot_incidents, market):
        halted = True

    mode = "PAPER"
    if spec["mode_env"]:
        mode = _normalize_mode(os.environ.get(spec["mode_env"]))

    as_of = (health or {}).get("as_of") or _now()
    if hasattr(as_of, "isoformat"):
        as_of = as_of.isoformat()

    unknown_n = sum(
        1
        for t in bot_tasks
        if t.get("status") in _UNKNOWN or t.get("reconciliation_status") == "UNKNOWN"
    )
    open_n = sum(1 for t in bot_tasks if t.get("status") in _ACTIVE_ORDER)
    return {
        "bot_id": spec["bot_id"],
        "label": spec["label"],
        "market": market,
        "read_only": spec["read_only"],
        "as_of": as_of,
        "runtime": _health_runtime(health),
        "mode": mode,
        "data": _health_data(health),
        "task": _task_dimension(bot_tasks),
        "order": "NONE" if spec["read_only"] else _order_dimension(bot_tasks),
        "risk": _risk_dimension(health, bot_tasks, bot_incidents, halted),
        "clock_skew_ms": (health or {}).get("clock_skew_ms"),
        "degraded": bool((health or {}).get("degraded")) if health else True,
        "detail": None if health is None else health.get("detail"),
        "connection": "DISCONNECTED" if health is None else "CONNECTED",
        "counts": {
            "pending_approvals": 0
            if spec["read_only"]
            else _approvals_count(approvals, market),
            "open_orders": 0 if spec["read_only"] else open_n,
            "unknown_orders": 0 if spec["read_only"] else unknown_n,
            "incidents": len(
                [
                    i
                    for i in bot_incidents
                    if i.get("event_type")
                    in (
                        "incident/opened",
                        "account/mismatch",
                        "kill_switch/succeeded",
                    )
                ]
            ),
        },
    }


def _aggregate_chief(crypto: dict, ashare: dict, all_tasks: list[dict]) -> dict:
    runtimes = {crypto["runtime"], ashare["runtime"]}
    if runtimes == {"OFFLINE"}:
        runtime = "OFFLINE"
    elif "OFFLINE" in runtimes or "DEGRADED" in runtimes:
        runtime = "DEGRADED"
    else:
        runtime = "ONLINE"

    datas = {crypto["data"], ashare["data"]}
    if datas == {"DISCONNECTED"}:
        data = "DISCONNECTED"
    elif "DISCONNECTED" in datas or "STALE" in datas:
        data = "STALE"
    else:
        data = "FRESH"

    risks = {crypto["risk"], ashare["risk"]}
    risk = "NORMAL"
    for candidate in ("HALTED", "INCIDENT", "WARNING", "NORMAL"):
        if candidate in risks:
            risk = candidate
            break

    modes = [crypto["mode"], ashare["mode"]]
    as_of = max(str(crypto.get("as_of") or ""), str(ashare.get("as_of") or ""))
    skews = [
        v
        for v in (crypto.get("clock_skew_ms"), ashare.get("clock_skew_ms"))
        if isinstance(v, int)
    ]
    details = [d for d in (crypto.get("detail"), ashare.get("detail")) if d]
    return {
        "bot_id": "market-chief",
        "label": "Market Chief",
        "market": None,
        "read_only": True,
        "as_of": as_of or _now(),
        "runtime": runtime,
        "mode": global_mode(modes),
        "data": data,
        "task": _task_dimension(all_tasks),
        "order": "NONE",
        "risk": risk,
        "clock_skew_ms": max(skews) if skews else None,
        "degraded": runtime != "ONLINE" or data != "FRESH" or risk != "NORMAL",
        "detail": "; ".join(details) or None,
        "connection": "DISCONNECTED" if runtime == "OFFLINE" else "CONNECTED",
        "counts": {
            "pending_approvals": crypto["counts"]["pending_approvals"]
            + ashare["counts"]["pending_approvals"],
            "open_orders": 0,
            "unknown_orders": crypto["counts"]["unknown_orders"]
            + ashare["counts"]["unknown_orders"],
            "incidents": crypto["counts"]["incidents"] + ashare["counts"]["incidents"],
        },
    }


def build_overview(fetch: FetchJson | None = None) -> dict[str, Any]:
    from projection_api.main import get_bot_tasks, get_incidents

    fetch = fetch or default_gateway_fetch
    tasks = get_bot_tasks()
    incidents = get_incidents(50)
    approvals = fetch("/v1/approvals?status=REQUESTED")
    crypto_health = fetch("/v1/markets/CRYPTO/health")
    ashare_health = fetch("/v1/markets/A_SHARE/health")
    if not isinstance(crypto_health, dict):
        crypto_health = None
    if not isinstance(ashare_health, dict):
        ashare_health = None
    if not isinstance(approvals, list):
        approvals = []

    crypto = _build_bot(BOTS[1], crypto_health, tasks, incidents, approvals)
    ashare = _build_bot(BOTS[2], ashare_health, tasks, incidents, approvals)
    chief = _aggregate_chief(crypto, ashare, tasks)
    bots = [chief, crypto, ashare]
    modes = [crypto["mode"], ashare["mode"]]
    alerts: list[str] = []
    for bot in bots:
        if bot["risk"] == "HALTED":
            alerts.append(f"{bot['label']} HALTED")
        if bot["order"] == "UNKNOWN" or bot["counts"]["unknown_orders"]:
            alerts.append(f"{bot['label']} UNKNOWN")
        if bot["data"] == "STALE":
            alerts.append(f"{bot['label']} STALE")
    live_anomaly = any(bot["mode"] == "LIVE" for bot in (crypto, ashare))
    if live_anomaly:
        alerts.append("LIVE anomaly (not selectable)")
    return {
        "as_of": _now(),
        "global_mode": global_mode(modes),
        "live_anomaly": live_anomaly,
        "alerts": list(dict.fromkeys(alerts)),
        "bots": bots,
    }
