import json
import os
import re
import sqlite3

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from projection_api.auth import require_projection_read

app = FastAPI(
    title="Projection API",
    description="面向前端的只读投影。不处理资金动作。",
    version="0.3.0",
)

QUANT_GATEWAY_URL = os.environ.get("QUANT_GATEWAY_URL", "http://127.0.0.1:8001")
QUANT_GATEWAY_API_KEY = os.environ.get("QUANT_GATEWAY_API_KEY", "")
STRATEGY_EVOLUTION_URL = os.environ.get(
    "STRATEGY_EVOLUTION_URL", "http://127.0.0.1:8002"
)
_ACTION_RE = re.compile(
    r"(帮我|请你|立刻|现在).{0,12}(批准|拒绝|下单|撤单|紧急停止|kill)",
    re.IGNORECASE,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "projection-api"}


async def _proxy_get(base_url: str, path: str, params: dict | None = None):
    """透传上游只读响应。

    上游状态码必须原样传递：503 表示失败关闭，不能被压成 500，
    否则前端无法区分「网关拒绝」和「投影服务自身故障」。
    """
    headers = {}
    if QUANT_GATEWAY_API_KEY and base_url.rstrip("/") == QUANT_GATEWAY_URL.rstrip("/"):
        headers["X-API-Key"] = QUANT_GATEWAY_API_KEY
    # 策略进化服务鉴权(内部服务,生产失败关闭)
    if base_url.rstrip("/") == STRATEGY_EVOLUTION_URL.rstrip("/"):
        evolution_key = os.environ.get("STRATEGY_EVOLUTION_API_KEY")
        if evolution_key:
            headers["X-API-Key"] = evolution_key
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{base_url}{path}", params=params, headers=headers or None
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"upstream unreachable: {exc}") from exc
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def _proxy_gateway(path: str, params: dict | None = None):
    return await _proxy_get(QUANT_GATEWAY_URL, path, params)


async def _proxy_evolution(path: str, params: dict | None = None):
    return await _proxy_get(STRATEGY_EVOLUTION_URL, path, params)


@app.get("/v1/bots/overview")
def get_bots_overview(_auth: None = Depends(require_projection_read)):
    """三个 Bot 的六维只读状态。不触发资金动作。要求服务身份。"""
    from projection_api.overview import build_overview

    return build_overview()


@app.get("/v1/markets/{market}/health")
async def get_health(market: str, _auth: None = Depends(require_projection_read)):
    return await _proxy_gateway(f"/v1/markets/{market}/health")


@app.get("/v1/markets/{market}/positions")
async def get_positions(market: str, _auth: None = Depends(require_projection_read)):
    return await _proxy_gateway(f"/v1/markets/{market}/positions")


@app.get("/v1/markets/{market}/accounts")
async def get_accounts(market: str, _auth: None = Depends(require_projection_read)):
    return await _proxy_gateway(f"/v1/markets/{market}/accounts")


@app.get("/v1/markets/{market}/signals")
async def get_signals(market: str, _auth: None = Depends(require_projection_read)):
    return await _proxy_gateway(f"/v1/markets/{market}/signals")


@app.get("/v1/markets/{market}/watch")
async def get_watch(market: str, _auth: None = Depends(require_projection_read)):
    return await _proxy_gateway(f"/v1/markets/{market}/watch")


@app.get("/v1/approvals")
async def get_approvals(
    status: str | None = None,
    market: str | None = None,
    _auth: None = Depends(require_projection_read),
):
    params = {k: v for k, v in {"status": status, "market": market}.items() if v}
    return await _proxy_gateway("/v1/approvals", params=params or None)


@app.get("/v1/experiments")
async def get_experiments(
    market: str | None = None,
    _auth: None = Depends(require_projection_read),
):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/experiments", params=params)


@app.get("/v1/candidates")
async def get_candidates(
    market: str | None = None,
    _auth: None = Depends(require_projection_read),
):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/candidates", params=params)


@app.get("/v1/markets/{market}/orders/{order_id}")
async def get_order(
    market: str,
    order_id: str,
    _auth: None = Depends(require_projection_read),
):
    return await _proxy_gateway(f"/v1/markets/{market}/orders/{order_id}")


@app.get("/v1/bot-tasks")
def get_bot_tasks(
    bot: str | None = None,
    status: str | None = None,
    _auth: None = Depends(require_projection_read),
):
    """只读投影 Runtime 任务，供异常路径与对账状态展示。"""
    db = os.environ.get("DSH_RUNTIME_DB", "")
    if not db or db.startswith(":memory"):
        return []
    if not os.path.isfile(db):
        return []
    conn = sqlite3.connect(db)
    sql = (
        "SELECT task_id, bot, kind, status, subject_id, approval_id, order_id,"
        " COALESCE(reconciliation_status, 'PENDING'), payload, updated_at"
        " FROM bot_tasks"
    )
    params: list = []
    clauses = []
    if bot:
        clauses.append("bot = ?")
        params.append(bot)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "task_id": r[0],
            "bot": r[1],
            "kind": r[2],
            "status": r[3],
            "subject_id": r[4],
            "approval_id": r[5],
            "order_id": r[6],
            "reconciliation_status": r[7],
            "payload": json.loads(r[8]) if r[8] else {},
            "updated_at": r[9],
        }
        for r in rows
    ]


def _runtime_conn():
    db = os.environ.get("DSH_RUNTIME_DB", "")
    if not db or db.startswith(":memory") or not os.path.isfile(db):
        return None
    return sqlite3.connect(db)


def _fetch_runtime_rows(sql: str, params: tuple = ()) -> list[tuple]:
    conn = _runtime_conn()
    if conn is None:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _attention_items() -> list[dict]:
    try:
        rows = _fetch_runtime_rows(
            "SELECT market, symbol, title, action, importance, direction, confidence"
            " FROM intelligence_items ORDER BY importance DESC, observed_at DESC LIMIT 5"
        )
    except sqlite3.Error:
        return []
    return [
        {
            "market": row[0],
            "symbol": row[1],
            "title": row[2],
            "action": row[3],
            "importance": row[4],
            "direction": row[5],
            "confidence": row[6],
        }
        for row in rows
    ]


def _decision_ledger_rows(limit: int = 200) -> list[dict]:
    try:
        rows = _fetch_runtime_rows(
            "SELECT decision_id, bot, market, symbol, status, signal_id, strategy_id,"
            " strategy_version, task_id, approval_id, order_id, fill_id, audit_id,"
            " action, entry_plan, exit_plan, evidence_refs, payload, created_at"
            " FROM decision_ledger ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    except sqlite3.Error:
        return []
    return [
        {
            "decision_id": row[0],
            "bot": row[1],
            "market": row[2],
            "symbol": row[3],
            "status": row[4],
            "signal_id": row[5],
            "strategy_id": row[6],
            "strategy_version": row[7],
            "task_id": row[8],
            "approval_id": row[9],
            "order_id": row[10],
            "fill_id": row[11],
            "audit_id": row[12],
            "action": row[13],
            "entry_plan": json.loads(row[14] or "{}"),
            "exit_plan": json.loads(row[15] or "{}"),
            "evidence_refs": json.loads(row[16] or "[]"),
            "payload": json.loads(row[17] or "{}"),
            "created_at": row[18],
        }
        for row in rows
    ]


def _pipeline_candidates() -> list[dict]:
    try:
        rows = _fetch_runtime_rows(
            "SELECT payload FROM audit_reports"
            " WHERE report_kind = 'optimization-daily'"
            " ORDER BY created_at DESC LIMIT 2"
        )
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            continue
        out.extend(item for item in (payload.get("candidates") or []) if item.get("can_apply") is False)
    return out


def _decision_ledger_coverage(rows: list[dict]) -> dict[str, int]:
    reconciled = 0
    for row in rows:
        reconciliation = (row.get("payload") or {}).get("reconciliation") or {}
        if reconciliation.get("reconciliation_status") == "MATCHED":
            reconciled += 1
    return {
        "decisions": len(rows),
        "approved": sum(1 for row in rows if row.get("approval_id")),
        "ordered": sum(1 for row in rows if row.get("order_id")),
        "filled": sum(1 for row in rows if row.get("fill_id")),
        "reconciled": reconciled,
        "audited": sum(1 for row in rows if row.get("audit_id")),
    }


_KILL_SWITCH_AUDIT = {
    "kill_switch.requested": "kill_switch/requested",
    "kill_switch.succeeded": "kill_switch/succeeded",
    "kill_switch.failed": "kill_switch/failed",
    "kill_switch.resumed": "kill_switch/resumed",
}


def _runtime_incidents(limit: int) -> list[dict]:
    conn = _runtime_conn()
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT event_id, event_type, occurred_at, market, actor_kind, actor_id, payload"
        " FROM domain_events WHERE event_type IN"
        " ('incident/opened','account/mismatch')"
        " ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "event_id": r[0],
            "event_type": r[1],
            "occurred_at": r[2],
            "market": r[3],
            "actor": {"kind": r[4], "id": r[5]},
            "payload": json.loads(r[6]) if r[6] else {},
            "source": "runtime",
        }
        for r in rows
    ]


def _gateway_kill_switch_incidents(limit: int) -> list[dict]:
    """Kill Switch 以 Gateway audit 为权威源，不写 Runtime domain_events。"""
    headers = (
        {"X-API-Key": QUANT_GATEWAY_API_KEY} if QUANT_GATEWAY_API_KEY else None
    )
    try:
        resp = httpx.get(
            f"{QUANT_GATEWAY_URL}/v1/audit",
            params={"limit": min(limit, 200)},
            headers=headers,
            timeout=2.0,
        )
    except Exception:
        return []
    if not resp.is_success:
        return []
    body = resp.json()
    if not isinstance(body, list):
        return []
    rows = []
    for item in body:
        event_type = _KILL_SWITCH_AUDIT.get(item.get("action") or "")
        if not event_type:
            continue
        rows.append(
            {
                "event_id": item.get("audit_id"),
                "event_type": event_type,
                "occurred_at": item.get("occurred_at"),
                "market": item.get("market"),
                "actor": {
                    "kind": "human",
                    "id": item.get("actor_principal") or item.get("service_principal"),
                },
                "payload": {
                    "reason": item.get("detail") or item.get("action"),
                    "subject_id": item.get("subject_id"),
                    "outcome": item.get("outcome"),
                    "source": "gateway-audit",
                },
                "source": "gateway-audit",
            }
        )
    return rows


@app.get("/v1/incidents")
def get_incidents(limit: int = 50, _auth: None = Depends(require_projection_read)):
    """Runtime 事故 + Gateway audit 中的 Kill Switch。"""
    merged = _runtime_incidents(limit) + _gateway_kill_switch_incidents(limit)
    merged.sort(key=lambda row: row.get("occurred_at") or "", reverse=True)
    return merged[:limit]


@app.get("/v1/shadow-decisions")
def get_shadow_decisions(
    bot: str | None = None,
    _auth: None = Depends(require_projection_read),
):
    """只读 Shadow 决策。不触发审批或下单。"""
    rows = get_bot_tasks(bot=bot, status="SHADOW_RECORDED")
    decisions = []
    for task in rows:
        payload = task.get("payload") or {}
        decision = payload.get("shadow_decision") or {}
        decisions.append(
            {
                "task_id": task["task_id"],
                "bot": task["bot"],
                "signal_id": task.get("subject_id"),
                "market": payload.get("market"),
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "status": task["status"],
                "updated_at": task.get("updated_at"),
                **decision,
            }
        )
    return decisions


@app.get("/v1/intelligence/feed")
def get_intelligence_feed(
    bot: str | None = None,
    market: str | None = None,
    limit: int = 50,
    _auth: None = Depends(require_projection_read),
):
    sql = (
        "SELECT item_id, bot, market, source_id, symbol, title, source_url,"
        " published_at, observed_at, authority, direction, horizon,"
        " importance, confidence, action, payload"
        " FROM intelligence_items"
    )
    clauses = []
    params: list = []
    if bot:
        clauses.append("bot = ?")
        params.append(bot)
    if market:
        clauses.append("market = ?")
        params.append(market)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY observed_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 200))
    rows = _fetch_runtime_rows(sql, tuple(params))
    return [
        {
            "item_id": r[0],
            "bot": r[1],
            "market": r[2],
            "source_id": r[3],
            "symbol": r[4],
            "title": r[5],
            "source_url": r[6],
            "published_at": r[7],
            "observed_at": r[8],
            "authority": r[9],
            "direction": r[10],
            "horizon": r[11],
            "importance": r[12],
            "confidence": r[13],
            "action": r[14],
            "payload": json.loads(r[15]) if r[15] else {},
        }
        for r in rows
    ]


@app.get("/v1/audit/reports")
def get_audit_reports(
    bot: str | None = None,
    report_kind: str | None = None,
    limit: int = 20,
    _auth: None = Depends(require_projection_read),
):
    sql = (
        "SELECT report_id, bot, market, report_kind, period_key, created_at, payload"
        " FROM audit_reports"
    )
    clauses = []
    params: list = []
    if bot:
        clauses.append("bot = ?")
        params.append(bot)
    if report_kind:
        clauses.append("report_kind = ?")
        params.append(report_kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 100))
    rows = _fetch_runtime_rows(sql, tuple(params))
    return [
        {
            "report_id": r[0],
            "bot": r[1],
            "market": r[2],
            "report_kind": r[3],
            "period_key": r[4],
            "created_at": r[5],
            "payload": json.loads(r[6]) if r[6] else {},
        }
        for r in rows
    ]


def _first_account(rows) -> dict | None:
    if isinstance(rows, list) and rows:
        return rows[0] if isinstance(rows[0], dict) else None
    return None


def _safe_list(value) -> list:
    return value if isinstance(value, list) else []


@app.get("/v1/today")
def get_today(_auth: None = Depends(require_projection_read)):
    """首页作战板：人话结论。不触发资金动作。"""
    from concurrent.futures import ThreadPoolExecutor

    from projection_api.today import build_today

    headers = (
        {"X-API-Key": QUANT_GATEWAY_API_KEY} if QUANT_GATEWAY_API_KEY else None
    )
    paths = (
        "/v1/markets/CRYPTO/health",
        "/v1/markets/A_SHARE/health",
        "/v1/markets/CRYPTO/accounts",
        "/v1/markets/A_SHARE/accounts",
        "/v1/markets/CRYPTO/signals",
        "/v1/markets/A_SHARE/signals",
        "/v1/markets/CRYPTO/watch",
        "/v1/markets/A_SHARE/watch",
    )

    def _get(path: str):
        try:
            resp = httpx.get(f"{QUANT_GATEWAY_URL}{path}", headers=headers, timeout=2.0)
        except Exception:
            return None
        return resp.json() if resp.is_success else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = dict(zip(paths, pool.map(_get, paths), strict=False))
    return build_today(
        crypto_health=fetched[paths[0]] if isinstance(fetched[paths[0]], dict) else None,
        ashare_health=fetched[paths[1]] if isinstance(fetched[paths[1]], dict) else None,
        crypto_account=_first_account(fetched[paths[2]]),
        ashare_account=_first_account(fetched[paths[3]]),
        crypto_signals=_safe_list(fetched[paths[4]]),
        ashare_signals=_safe_list(fetched[paths[5]]),
        crypto_watch=fetched[paths[6]] or {},
        ashare_watch=fetched[paths[7]] or {},
        decisions=get_shadow_decisions(),
        attention=_attention_items(),
    )


def _snapshot_extra(market: str) -> dict:
    root = os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or ""
    path = os.path.join(root, f"{market}.json") if root else ""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@app.get("/v1/trade-quality")
def get_trade_quality(_auth: None = Depends(require_projection_read)):
    """交易质量审计。只读，不改策略，不下单。"""
    from datetime import UTC, datetime

    from projection_api.trade_quality import build_trade_quality_report

    headers = (
        {"X-API-Key": QUANT_GATEWAY_API_KEY} if QUANT_GATEWAY_API_KEY else None
    )

    def _get(path: str):
        try:
            resp = httpx.get(f"{QUANT_GATEWAY_URL}{path}", headers=headers, timeout=2.0)
        except Exception:
            return None
        return resp.json() if resp.is_success else None

    ashare_snap = _snapshot_extra("A_SHARE")
    crypto_snap = _snapshot_extra("CRYPTO")
    ledger_rows = _decision_ledger_rows()
    return build_trade_quality_report(
        decisions=get_shadow_decisions(),
        crypto_account=_first_account(_get("/v1/markets/CRYPTO/accounts")),
        ashare_account=_first_account(_get("/v1/markets/A_SHARE/accounts")),
        crypto_positions=_safe_list(_get("/v1/markets/CRYPTO/positions")),
        ashare_positions=_safe_list(_get("/v1/markets/A_SHARE/positions")),
        ashare_fills=_safe_list(ashare_snap.get("fills")),
        crypto_fills=_safe_list(crypto_snap.get("fills")),
        closed_trades=_safe_list(crypto_snap.get("closed_trades")),
        equity_curve=_safe_list(crypto_snap.get("equity_curve")),
        ledger_rows=ledger_rows,
        ledger_coverage=_decision_ledger_coverage(ledger_rows),
        pipeline_candidates=_pipeline_candidates(),
        as_of=datetime.now(UTC).isoformat(),
    )


@app.get("/v1/intelligence")
def get_intelligence(_auth: None = Depends(require_projection_read)):
    """事件情报投影。只读 Shadow，不把事件当成下单信号。"""
    empty = {
        "as_of": None,
        "mode": "SHADOW",
        "disclaimer": "只进入 Shadow。没有原文存证的记录不会评分。不能直接下单。",
        "documents": [],
        "events": [],
        "coverage": {
            "x_stream": False,
            "us_quotes": False,
            "cninfo": False,
            "playwright": False,
        },
    }
    root = os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or ""
    path = os.path.join(root, "INTELLIGENCE.json") if root else ""
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(payload, dict):
        return empty
    payload.setdefault("mode", "SHADOW")
    payload.setdefault("disclaimer", empty["disclaimer"])
    payload.setdefault("documents", [])
    payload.setdefault("events", [])
    payload["as_of"] = payload.get("exported_at")
    return payload


@app.get("/v1/chief/briefing")
def get_chief_briefing(_auth: None = Depends(require_projection_read)):
    """Chief 每日作战简报（最新一条）。"""
    conn = _runtime_conn()
    if conn is None:
        return {"as_of": None, "focus": [], "risks": [], "abandons": [], "counts": {}}
    row = conn.execute(
        "SELECT content, created_at FROM agent_memory"
        " WHERE bot = 'market-chief' AND kind = 'daily-briefing'"
        " ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return {"as_of": None, "focus": [], "risks": [], "abandons": [], "counts": {}}
    try:
        body = json.loads(row[0])
    except json.JSONDecodeError:
        body = {"text": row[0]}
    if isinstance(body, dict):
        body.setdefault("as_of", row[1])
        return body
    return {"as_of": row[1], "text": row[0]}


class ChiefQuery(BaseModel):
    question: str


@app.post("/v1/chief/query")
def chief_query(
    body: ChiefQuery, _auth: None = Depends(require_projection_read)
) -> dict:
    """只读解释/查询。不批准、不风控、不下单。"""
    question = (body.question or "").strip()
    if not question:
        return {"role": "chief", "refused": False, "text": "请输入要查询的问题。"}
    if _ACTION_RE.search(question):
        return {
            "role": "chief",
            "refused": True,
            "text": "我不能执行批准、风控或下单。请到审批页由人工决定，或到事故页查看 Kill Switch。",
        }
    today = get_today()
    tasks = get_bot_tasks()
    incidents = get_incidents(20)
    lines_health: list[str] = []
    for market in ("CRYPTO", "A_SHARE"):
        try:
            health = httpx.get(
                f"{QUANT_GATEWAY_URL}/v1/markets/{market}/health",
                headers=(
                    {"X-API-Key": QUANT_GATEWAY_API_KEY}
                    if QUANT_GATEWAY_API_KEY
                    else None
                ),
                timeout=2.0,
            )
            if health.is_success:
                payload = health.json()
                ok = payload.get("system_ok") and payload.get("data_fresh")
                lines_health.append(
                    f"{market} {'正常' if ok else '降级'}（fresh={payload.get('data_fresh')}）。"
                )
            else:
                lines_health.append(f"{market} 健康检查 {health.status_code}。")
        except Exception:
            lines_health.append(f"{market} 健康检查不可达。")
    open_incidents = [
        i for i in incidents if i["event_type"] in ("incident/opened", "account/mismatch")
    ]
    incident_tasks = [t for t in tasks if t["status"] == "INCIDENT"]
    awaiting = [t for t in tasks if t["status"] == "AWAITING_APPROVAL"]
    done = [t for t in tasks if t["status"] == "DONE"]
    lines = [
        today.get("headline") or "",
        *[
            f"{story.get('title')}。{' '.join(story.get('points') or [])}"
            if isinstance(story, dict)
            else str(story)
            for story in (today.get("stories") or [])
        ],
        today.get("disclaimer") or "",
    ] + lines_health + [
        f"当前任务 {len(tasks)} 条：待审批 {len(awaiting)}，已完成 {len(done)}，事故 {len(incident_tasks)}。",
        f"最近事故事件 {len(open_incidents)} 条。",
    ]
    if incident_tasks:
        sample = incident_tasks[0]
        reason = (sample.get("payload") or {}).get("reason") or sample.get("reconciliation_status")
        lines.append(f"最近事故任务 {sample['task_id']} 状态 {sample['status']}（{reason}）。")
    if "对账" in question or "mismatch" in question.lower():
        mismatch = [t for t in tasks if t.get("reconciliation_status") == "MISMATCH"]
        lines.append(f"对账 MISMATCH 任务 {len(mismatch)} 条。")
    if "审批" in question:
        lines.append(f"待人工审批任务 {len(awaiting)} 条；决定请走审批页，不要让我代批。")
    lines.append("以上来自 Runtime 投影，不含资金动作。")
    return {"role": "chief", "refused": False, "text": "\n".join(lines)}


# Prometheus 指标：infra/observability/prometheus.yml 抓取 /metrics
from prometheus_client import make_asgi_app  # noqa: E402

app.mount("/metrics", make_asgi_app())
