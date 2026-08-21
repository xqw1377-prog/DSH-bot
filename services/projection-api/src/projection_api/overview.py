"""三 Bot 只读总览。不触发资金动作，不写 Gateway。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time
from typing import Any
from collections.abc import Callable
from zoneinfo import ZoneInfo

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
_ANALYZING = {"SIGNAL_RECEIVED", "PREVIEWED", "RUNNING", "SHADOW_RECORDED"}
_UNKNOWN = {
    "SUBMISSION_UNKNOWN",
    "APPROVAL_UNKNOWN",
    "UNKNOWN",
}
_REJECTED = {"ORDER_REJECTED", "REJECTED"}
SEVERITY_ORDER = (
    "HALTED",
    "INCIDENT",
    "UNKNOWN",
    "DEGRADED",
    "WARNING",
    "NORMAL",
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_A_SHARE_SESSIONS = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_mode(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return "UNKNOWN"
    value = str(raw).strip().lower()
    if value == "paper":
        return "PAPER"
    if value == "shadow":
        return "SHADOW"
    if value == "live":
        return "LIVE"
    return "UNKNOWN"


def global_mode(modes: list[str]) -> str:
    if "LIVE" in modes:
        return "SECURITY_VIOLATION"
    if "UNKNOWN" in modes:
        return "UNKNOWN"
    unique = {m for m in modes if m in {"PAPER", "SHADOW"}}
    if unique == {"PAPER"}:
        return "PAPER"
    if unique == {"SHADOW"}:
        return "SHADOW"
    if unique == {"PAPER", "SHADOW"}:
        return "MIXED"
    return "UNKNOWN"


def pick_severity(*values: str) -> str:
    rank = {name: index for index, name in enumerate(SEVERITY_ORDER)}
    present = [value for value in values if value in rank]
    if not present:
        return "NORMAL"
    return min(present, key=lambda value: rank[value])


def a_share_session_open(now: datetime | None = None) -> bool:
    if now is None:
        current = datetime.now(_SHANGHAI)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=UTC).astimezone(_SHANGHAI)
    else:
        current = now.astimezone(_SHANGHAI)
    if current.weekday() >= 5:
        return False
    clock = current.time()
    return any(start <= clock < end for start, end in _A_SHARE_SESSIONS)


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
    if not health.get("system_ok"):
        return "DEGRADED"
    return "ONLINE"


def _health_data(
    health: dict | None,
    market: str | None,
    now: datetime | None,
) -> str:
    if health is None:
        return "DISCONNECTED"
    if not health.get("data_fresh"):
        return "STALE"
    if market == "A_SHARE" and not a_share_session_open(now):
        return "MARKET_CLOSED"
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


_RECOVERABLE_INCIDENT_REASONS = {
    "market degraded or unreachable",
    "market data degraded",
}


def _incident_reason(row: dict) -> str:
    payload = row.get("payload") or {}
    return str(payload.get("reason") or "")


def _active_incidents(incidents: list[dict], health: dict | None) -> list[dict]:
    """历史「数据降级」在当前健康已恢复时不再算未决事故。"""
    active: list[dict] = []
    recovered = bool(
        health
        and health.get("system_ok")
        and health.get("data_fresh")
    )
    for row in incidents:
        event = row.get("event_type")
        if event == "account/mismatch":
            active.append(row)
            continue
        if event != "incident/opened":
            continue
        if recovered and _incident_reason(row) in _RECOVERABLE_INCIDENT_REASONS:
            continue
        active.append(row)
    return active


def _risk_dimension(
    health: dict | None,
    tasks: list[dict],
    incidents: list[dict],
    halted: bool,
    market: str | None = None,
    now: datetime | None = None,
) -> str:
    if halted:
        return "HALTED"
    if any(t.get("status") == "INCIDENT" for t in tasks):
        return "INCIDENT"
    if _active_incidents(incidents, health):
        return "INCIDENT"
    if health is None:
        return "WARNING"
    if health.get("degraded"):
        return "WARNING"
    market_closed = market == "A_SHARE" and not a_share_session_open(now)
    if not health.get("data_fresh") and not market_closed:
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


def _bot_severity(bot: dict[str, Any]) -> str:
    unknown = (
        bot["mode"] == "UNKNOWN"
        or bot["order"] == "UNKNOWN"
        or bool(bot["counts"]["unknown_orders"])
    )
    return pick_severity(
        bot["risk"] if bot["risk"] in SEVERITY_ORDER else "NORMAL",
        "UNKNOWN" if unknown else "NORMAL",
        "DEGRADED" if bot["runtime"] in {"DEGRADED", "OFFLINE"} else "NORMAL",
        "WARNING" if bot["data"] == "STALE" else "NORMAL",
    )


def _build_bot(
    spec: dict,
    health: dict | None,
    tasks: list[dict],
    incidents: list[dict],
    approvals: list | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    market = spec["market"]
    bot_tasks = _filter_tasks(tasks, spec["runtime_bot"])
    bot_incidents = _filter_incidents(incidents, market)
    halted = False
    if health is not None and "emergency stop" in (health.get("detail") or "").lower():
        halted = True
    if _kill_switch_halted(bot_incidents, market):
        halted = True

    mode = "UNKNOWN"
    if spec["mode_env"]:
        mode = _normalize_mode(os.environ.get(spec["mode_env"]))

    as_of = (health or {}).get("as_of") or _now()
    if hasattr(as_of, "isoformat"):
        as_of = as_of.isoformat()
    observed = None if health is None else health.get("source_observed_at")
    if hasattr(observed, "isoformat"):
        observed = observed.isoformat()
    exported = None if health is None else health.get("exported_at")
    if hasattr(exported, "isoformat"):
        exported = exported.isoformat()

    unknown_n = sum(
        1
        for t in bot_tasks
        if t.get("status") in _UNKNOWN or t.get("reconciliation_status") == "UNKNOWN"
    )
    open_n = sum(1 for t in bot_tasks if t.get("status") in _ACTIVE_ORDER)
    bot = {
        "bot_id": spec["bot_id"],
        "label": spec["label"],
        "market": market,
        "read_only": spec["read_only"],
        "as_of": as_of,
        "runtime": _health_runtime(health),
        "mode": mode,
        "data": _health_data(health, market, now),
        "task": _task_dimension(bot_tasks),
        "order": "NONE" if spec["read_only"] else _order_dimension(bot_tasks),
        "risk": _risk_dimension(
            health, bot_tasks, bot_incidents, halted, market, now
        ),
        "clock_skew_ms": (health or {}).get("clock_skew_ms"),
        "degraded": bool((health or {}).get("degraded")) if health else True,
        "detail": None if health is None else health.get("detail"),
        "source_system": None if health is None else health.get("source_system"),
        "source_mode": None if health is None else health.get("source_mode"),
        "source_observed_at": observed,
        "exported_at": exported,
        "snapshot_age_seconds": None if health is None else health.get("snapshot_age_seconds"),
        "export_age_seconds": None if health is None else health.get("export_age_seconds"),
        "connection": "DISCONNECTED" if health is None else "CONNECTED",
        "counts": {
            "pending_approvals": 0
            if spec["read_only"]
            else _approvals_count(approvals, market),
            "open_orders": 0 if spec["read_only"] else open_n,
            "unknown_orders": 0 if spec["read_only"] else unknown_n,
            "incidents": len(_active_incidents(bot_incidents, health))
            + sum(
                1
                for i in bot_incidents
                if i.get("event_type") == "kill_switch/succeeded"
            ),
        },
    }
    bot["severity"] = _bot_severity(bot)
    return bot


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
    elif "STALE" in datas:
        data = "STALE"
    elif "FRESH" in datas:
        data = "FRESH"
    elif "MARKET_CLOSED" in datas:
        data = "MARKET_CLOSED"
    else:
        data = "DISCONNECTED"

    risk = pick_severity(crypto["risk"], ashare["risk"])

    modes = [crypto["mode"], ashare["mode"]]
    as_of = max(str(crypto.get("as_of") or ""), str(ashare.get("as_of") or ""))
    skews = [
        v
        for v in (crypto.get("clock_skew_ms"), ashare.get("clock_skew_ms"))
        if isinstance(v, int)
    ]
    details = [d for d in (crypto.get("detail"), ashare.get("detail")) if d]
    chief = {
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
        "degraded": runtime != "ONLINE" or data == "STALE" or risk != "NORMAL",
        "detail": "; ".join(details) or None,
        "source_system": " / ".join(
            s
            for s in (crypto.get("source_system"), ashare.get("source_system"))
            if s
        ) or None,
        "source_mode": global_mode(modes),
        "source_observed_at": as_of or None,
        "exported_at": max(
            (str(v) for v in (crypto.get("exported_at"), ashare.get("exported_at")) if v),
            default=None,
        ),
        "snapshot_age_seconds": max(
            (
                v
                for v in (
                    crypto.get("snapshot_age_seconds"),
                    ashare.get("snapshot_age_seconds"),
                )
                if isinstance(v, int)
            ),
            default=None,
        ),
        "export_age_seconds": max(
            (
                v
                for v in (crypto.get("export_age_seconds"), ashare.get("export_age_seconds"))
                if isinstance(v, int)
            ),
            default=None,
        ),
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
    chief["severity"] = pick_severity(
        _bot_severity(chief), crypto["severity"], ashare["severity"]
    )
    return chief


def _safe_fetch(fetch: FetchJson, path: str) -> dict | list | None:
    try:
        return fetch(path)
    except Exception:
        return None


def build_overview(
    fetch: FetchJson | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from projection_api.main import get_bot_tasks, get_incidents

    fetch = fetch or default_gateway_fetch
    try:
        tasks = get_bot_tasks()
    except Exception:
        tasks = []
    try:
        incidents = get_incidents(50)
    except Exception:
        incidents = []
    approvals = _safe_fetch(fetch, "/v1/approvals?status=REQUESTED")
    crypto_health = _safe_fetch(fetch, "/v1/markets/CRYPTO/health")
    ashare_health = _safe_fetch(fetch, "/v1/markets/A_SHARE/health")
    if not isinstance(crypto_health, dict):
        crypto_health = None
    if not isinstance(ashare_health, dict):
        ashare_health = None
    if not isinstance(approvals, list):
        approvals = []

    crypto = _build_bot(BOTS[1], crypto_health, tasks, incidents, approvals, now)
    ashare = _build_bot(BOTS[2], ashare_health, tasks, incidents, approvals, now)
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
            age = bot.get("snapshot_age_seconds")
            age_text = f"（观察已停 {age}s）" if isinstance(age, int) else ""
            if bot.get("connection") == "CONNECTED":
                alerts.append(f"{bot['label']} 控制面正常、数据面 STALE{age_text}")
            else:
                alerts.append(f"{bot['label']} STALE{age_text}")
    live_anomaly = any(bot["mode"] == "LIVE" for bot in (crypto, ashare))
    if live_anomaly:
        alerts.append("GLOBAL MODE: SECURITY VIOLATION")
    return {
        "as_of": _now(),
        "global_mode": global_mode(modes),
        "live_anomaly": live_anomaly,
        "alerts": list(dict.fromkeys(alerts)),
        "bots": bots,
    }
