"""Market Chief：用户唯一主入口的 Bot 实现。

职责（全部只读 + 汇总，无资金动作）：
1. 逐市场查询 Gateway 健康状态，任何市场降级/不可达即发 incident 告警
2. 汇总全市场待人工审批事项为待办（提醒用户审批是当前卡点）
3. 汇总各 Bot 健康度：近期 tick 失败、未决事故、任务状态分布
   （来自 DSH Runtime 事件与任务账本，跨 Bot 同库只读）
4. 汇总与建议写入记忆（market-summary / todo / advice），事件留痕

Chief 不做具体订单决策——那是专业 Bot 的职责。
"""

import os

import httpx
from dsh_contracts import Market
from dsh_gateway_client import GatewayClient, GatewayError
from dsh_runtime import BotSession
from dsh_runtime.store import _get as _runtime_conn


class MarketChiefAgent:
    name = "market-chief"

    MARKETS = (Market.A_SHARE, Market.CRYPTO)
    # tick 失败只统计最近时间窗
    BOT_HEALTH_WINDOW_EVENTS = 200

    def __init__(self, gateway: GatewayClient, approvals=None):
        self.gateway = gateway
        self.approvals = approvals

    # Incident Center 地址（可选）；未配置时 Chief 不转发，只做本地汇总。
    # 转发失败不阻断 Chief 的只读汇总——事故闭环故障不能放大为主循环故障。
    @staticmethod
    def _incident_center_url() -> str:
        return os.environ.get("INCIDENT_CENTER_URL", "")

    def _forward_open_incidents(self, session: BotSession) -> None:
        """把 Runtime 账本中未决的 incident/opened 转发到事故中心。

        幂等：Incident Center 按指纹去重，重复转发安全。
        失败关闭于「转发」本身：记录错误记忆，绝不抛出、绝不影响汇总。
        """
        url = self._incident_center_url()
        if not url:
            return
        try:
            conn = _runtime_conn()
            rows = conn.execute(
                "SELECT actor_id, market, payload FROM domain_events"
                " WHERE event_type = 'incident/opened'"
                " ORDER BY occurred_at DESC LIMIT 50",
            ).fetchall()
        except Exception:
            return
        import json as _json
        for actor_id, market, payload in rows:
            data = _json.loads(payload)
            try:
                httpx.post(
                    url.rstrip("/") + "/v1/incidents",
                    json={
                        "source": actor_id,
                        "reason": data.get("reason", "unspecified"),
                        "subject": data.get("order_id")
                                   or data.get("task_id"),
                        "market": market if market != "GLOBAL" else None,
                        "severity": "HIGH" if "order" in (data.get("reason")
                                                          or "") else "NORMAL",
                    }, timeout=3.0,
                )
            except httpx.HTTPError as exc:
                session.memory.remember(
                    f"事故转发失败（{actor_id}）: {exc}；本地汇总不受影响",
                    kind="error", tags=["incident-forward-failed"],
                )
                return  # 中心不可达：本 tick 放弃转发，不重试风暴

    def tick(self, session: BotSession) -> None:
        summary = {"markets": {}, "pending_approvals": 0, "degraded": []}

        for market in self.MARKETS:
            session.use("query_health")
            try:
                health = self.gateway.get_health(market)
            except Exception:
                # 网关不可达或上游异常一律按降级处理：告警而不是猜测
                summary["markets"][market.value] = {"unreachable": True}
                summary["degraded"].append(market.value)
                continue
            ok = health.get("system_ok") and health.get("data_fresh")
            summary["markets"][market.value] = {
                "system_ok": health.get("system_ok"),
                "data_fresh": health.get("data_fresh"),
                "trading_channel_ok": health.get("trading_channel_ok"),
                "degraded": health.get("degraded"),
            }
            if not ok:
                summary["degraded"].append(market.value)

        session.use("approval_initiation")
        try:
            pending = self.gateway.list_approvals(status="REQUESTED")
            summary["pending_approvals"] = len(pending)
        except Exception:
            summary["pending_approvals"] = -1  # 未知，明确标注而非默认 0

        # 确定性事故闭环：转发未决事故到 Incident Center（幂等、失败不阻断）
        self._forward_open_incidents(session)

        # Bot 健康度：跨 Bot 只读 Runtime 账本（同库），不写入任何 Bot 状态
        summary["bots"] = self._bot_health()
        summary["open_incidents"] = self._open_incident_count()

        session.use("report_generation")
        self._write_summary(session, summary)
        session.events.emit(
            "market/chief.summary", "GLOBAL", "bot", self.name, summary
        )
        if summary["degraded"]:
            session.use("incident_alert")
            for market_name in summary["degraded"]:
                session.events.emit(
                    "incident/opened", market_name, "bot", self.name,
                    {"reason": "market degraded or unreachable",
                     "markets": summary["degraded"]},
                )

    def _bot_health(self) -> dict:
        """各 Bot 健康度：近期 tick 失败次数 + 任务状态分布。"""
        bots: dict[str, dict] = {}
        try:
            conn = _runtime_conn()
            rows = conn.execute(
                "SELECT event_type, actor_id, occurred_at FROM domain_events"
                " ORDER BY occurred_at DESC LIMIT ?",
                (self.BOT_HEALTH_WINDOW_EVENTS,),
            ).fetchall()
        except Exception:
            return bots  # 账本不可读：返回空汇总，不猜测
        for event_type, actor_id, occurred_at in rows:
            if actor_id in ("market-chief", "system", ""):
                continue
            entry = bots.setdefault(actor_id, {"tick_failed_recent": 0})
            if event_type == "bot/tick.failed":
                entry["tick_failed_recent"] += 1
        try:
            task_rows = conn.execute(
                "SELECT bot, status, COUNT(*) FROM bot_tasks"
                " GROUP BY bot, status"
            ).fetchall()
        except Exception:
            task_rows = []
        for bot, status, count in task_rows:
            bots.setdefault(bot, {})["tasks"] = (
                bots.setdefault(bot, {}).get("tasks", {}))
            bots[bot]["tasks"][status] = count
        for bot, entry in bots.items():
            if entry.get("tick_failed_recent", 0) > 0:
                entry["health"] = "degraded"
            else:
                entry["health"] = "ok"
        return bots

    def _open_incident_count(self) -> int:
        """未决事故：incident/opened 减去后续 resolved/mitigated（近似计数）。"""
        try:
            conn = _runtime_conn()
            opened = conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE event_type = 'incident/opened'"
            ).fetchone()[0]
            closed = conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE event_type IN"
                " ('incident/resolved', 'incident/mitigated')"
            ).fetchone()[0]
        except Exception:
            return -1  # 未知，明确标注而非默认 0
        return max(opened - closed, 0)

    def _write_summary(self, session: BotSession, summary: dict) -> None:
        parts = []
        for market, status in summary["markets"].items():
            if status.get("unreachable"):
                parts.append(f"{market} 不可达")
            elif market in summary["degraded"]:
                parts.append(f"{market} 降级")
            else:
                parts.append(f"{market} 正常")
        session.memory.remember(
            "；".join(parts), kind="market-summary", tags=["chief-summary"]
        )
        session.memory.remember(
            f"{summary['pending_approvals']} 项待审批",
            kind="todo",
            tags=["todo", "approvals"],
        )
        if summary["degraded"]:
            session.memory.remember(
                f"市场 {summary['degraded']} 状态降级，建议暂停相关 Bot 的新信号处理并关注恢复",
                kind="advice", tags=["advice", "degraded"],
            )


# 兼容别名：早期命名
MarketChief = MarketChiefAgent
