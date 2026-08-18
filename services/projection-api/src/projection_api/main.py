import json
import os
import re
import sqlite3

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{base_url}{path}", params=params, headers=headers or None
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"upstream unreachable: {exc}")
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def _proxy_gateway(path: str, params: dict | None = None):
    return await _proxy_get(QUANT_GATEWAY_URL, path, params)


async def _proxy_evolution(path: str, params: dict | None = None):
    return await _proxy_get(STRATEGY_EVOLUTION_URL, path, params)


@app.get("/v1/bots/overview")
def get_bots_overview():
    """三个 Bot 的六维只读状态。不触发资金动作。"""
    from projection_api.overview import build_overview

    return build_overview()


@app.get("/v1/markets/{market}/health")
async def get_health(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/health")


@app.get("/v1/markets/{market}/positions")
async def get_positions(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/positions")


@app.get("/v1/markets/{market}/accounts")
async def get_accounts(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/accounts")


@app.get("/v1/markets/{market}/signals")
async def get_signals(market: str):
    return await _proxy_gateway(f"/v1/markets/{market}/signals")


@app.get("/v1/approvals")
async def get_approvals(status: str | None = None, market: str | None = None):
    params = {k: v for k, v in {"status": status, "market": market}.items() if v}
    return await _proxy_gateway("/v1/approvals", params=params or None)


@app.get("/v1/experiments")
async def get_experiments(market: str | None = None):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/experiments", params=params)


@app.get("/v1/candidates")
async def get_candidates(market: str | None = None):
    params = {"market": market} if market else None
    return await _proxy_evolution("/v1/candidates", params=params)


@app.get("/v1/markets/{market}/orders/{order_id}")
async def get_order(market: str, order_id: str):
    return await _proxy_gateway(f"/v1/markets/{market}/orders/{order_id}")


@app.get("/v1/bot-tasks")
def get_bot_tasks(bot: str | None = None, status: str | None = None):
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
                "actor": {"kind": "human", "id": item.get("actor")},
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
def get_incidents(limit: int = 50):
    """Runtime 事故 + Gateway audit 中的 Kill Switch。"""
    merged = _runtime_incidents(limit) + _gateway_kill_switch_incidents(limit)
    merged.sort(key=lambda row: row.get("occurred_at") or "", reverse=True)
    return merged[:limit]


class ChiefQuery(BaseModel):
    question: str


@app.post("/v1/chief/query")
def chief_query(body: ChiefQuery) -> dict:
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
    lines = lines_health + [
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

