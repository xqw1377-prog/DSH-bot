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
from dsh_gateway_client import GatewayClient
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

    # Runtime 事件 reason（自由文本）→ 确定性规则 ID。
    # 指纹用 incident_type，reason 措辞变化不产生新事故。
    INCIDENT_RULES = {
        "order UNKNOWN beyond quarantine": "order_unknown_quarantine",
        "fill/position mismatch after FILLED": "fill_position_mismatch",
        "approval 404 but not locally confirmed expired":
            "approval_ledger_unknown",
        "degraded markets": "market_degraded",
        "market degraded or unreachable": "market_degraded",
        "reconciliation numeric inconsistency": "reconcile_numeric_mismatch",
    }

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
                "SELECT event_id, actor_id, market, payload FROM domain_events"
                " WHERE event_type = 'incident/opened'"
                " ORDER BY occurred_at DESC LIMIT 50",
            ).fetchall()
        except Exception:
            return
        import json as _json
        for event_id, actor_id, market, payload in rows:
            data = _json.loads(payload)
            reason = data.get("reason") or ""
            try:
                httpx.post(
                    url.rstrip("/") + "/v1/incidents",
                    json={
                        "source": actor_id,
                        "incident_type": self.INCIDENT_RULES.get(
                            reason, "uncategorized"),
                        "reason": reason or None,
                        "subject": data.get("order_id")
                                   or data.get("candidate_id")
                                   or data.get("task_id"),
                        "market": market if market != "GLOBAL" else None,
                        "severity": "HIGH" if "order" in reason
                                    or "mismatch" in reason else "NORMAL",
                        # 消息级幂等：Runtime 事件 ID，重复转发被中心忽略
                        "source_event_id": event_id,
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
        session.use("intelligence_review")
        summary["bots"] = self._bot_health()
        summary["open_incidents"] = self._open_incident_count()
        summary["intelligence"] = self._intelligence_summary()
        session.use("audit_review")
        summary["latest_audits"] = self._latest_audits()

        session.use("report_generation")
        self._write_summary(session, summary)
        self._write_daily_briefing(session)
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
        for event_type, actor_id, _occurred_at in rows:
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
        for _bot, entry in bots.items():
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
        intel = summary.get("intelligence") or {}
        if intel.get("high_priority", 0):
            session.memory.remember(
                f"高优先级情报 {intel['high_priority']} 条，已进入 Shadow 建议。",
                kind="todo",
                tags=["todo", "intelligence"],
            )
        if summary["degraded"]:
            session.memory.remember(
                f"市场 {summary['degraded']} 状态降级，建议暂停相关 Bot 的新信号处理并关注恢复",
                kind="advice", tags=["advice", "degraded"],
            )

    def _shadow_rows(self) -> list[dict]:
        import json as _json

        try:
            conn = _runtime_conn()
            rows = conn.execute(
                "SELECT bot, task_id, subject_id, payload, updated_at FROM bot_tasks"
                " WHERE status = 'SHADOW_RECORDED' ORDER BY updated_at DESC"
            ).fetchall()
        except Exception:
            return []
        items = []
        for bot, task_id, subject_id, payload, updated_at in rows:
            data = _json.loads(payload) if payload else {}
            decision = data.get("shadow_decision") or {}
            items.append(
                {
                    "bot": bot,
                    "task_id": task_id,
                    "signal_id": subject_id,
                    "market": data.get("market"),
                    "symbol": data.get("symbol"),
                    "side": data.get("side"),
                    "action": decision.get("action") or "HOLD",
                    "skip_reason": decision.get("skip_reason"),
                    "strength": decision.get("strength") or data.get("strength") or 0,
                    "quantity": decision.get("quantity"),
                    "suggested_price": decision.get("suggested_price"),
                    "outcome_price": decision.get("outcome_price"),
                    "simulated_pnl": decision.get("simulated_pnl"),
                    "why": decision.get("why"),
                    "why_not": decision.get("why_not"),
                    "disclaimer": decision.get("disclaimer") or "仅模拟，不会下单",
                    "updated_at": updated_at,
                }
            )
        return items

    def _intelligence_rows(self) -> list[dict]:
        import json as _json

        try:
            conn = _runtime_conn()
            rows = conn.execute(
                "SELECT bot, market, symbol, title, authority, direction, horizon,"
                " importance, confidence, action, payload, observed_at"
                " FROM intelligence_items ORDER BY observed_at DESC LIMIT 100"
            ).fetchall()
        except Exception:
            return []
        return [
            {
                "bot": r[0],
                "market": r[1],
                "symbol": r[2],
                "title": r[3],
                "authority": r[4],
                "direction": r[5],
                "horizon": r[6],
                "importance": r[7],
                "confidence": r[8],
                "action": r[9],
                "payload": _json.loads(r[10]) if r[10] else {},
                "observed_at": r[11],
            }
            for r in rows
        ]

    def _intelligence_summary(self) -> dict:
        rows = self._intelligence_rows()
        high = [row for row in rows if float(row.get("importance") or 0) >= 0.7]
        direct = [
            row for row in rows
            if row.get("action") in {"BUY", "SELL", "HOLD", "WATCH"}
        ]
        return {
            "total": len(rows),
            "high_priority": len(high),
            "top": direct[:5],
        }

    def _latest_audits(self) -> list[dict]:
        import json as _json

        try:
            conn = _runtime_conn()
            rows = conn.execute(
                "SELECT bot, report_kind, created_at, payload"
                " FROM audit_reports ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        except Exception:
            return []
        return [
            {
                "bot": r[0],
                "report_kind": r[1],
                "created_at": r[2],
                "payload": _json.loads(r[3]) if r[3] else {},
            }
            for r in rows
        ]

    def _focus_today(self, focus: list[dict], top_intel: list[dict], risks: list[dict]) -> list[dict]:
        items: list[dict] = []
        for row in focus:
            items.append(
                {
                    "kind": "shadow",
                    "market": row.get("market"),
                    "symbol": row.get("symbol"),
                    "title": f"{row.get('action')} {row.get('symbol')}",
                    "why": row.get("why"),
                    "interrupt": row.get("action") in {"BUY", "SELL"},
                }
            )
        for row in top_intel:
            items.append(
                {
                    "kind": "intelligence",
                    "market": row.get("market"),
                    "symbol": row.get("symbol"),
                    "title": row.get("title"),
                    "why": f"{row.get('direction')} / {row.get('action')}",
                    "interrupt": float(row.get("importance") or 0) >= 0.8,
                }
            )
        for row in risks[:2]:
            items.append(
                {
                    "kind": "risk",
                    "market": row.get("market"),
                    "symbol": row.get("symbol"),
                    "title": row.get("skip_reason") or "风险",
                    "why": row.get("why"),
                    "interrupt": True,
                }
            )
        seen: set[str] = set()
        unique: list[dict] = []
        for row in items:
            key = f"{row.get('kind')}:{row.get('market')}:{row.get('symbol')}:{row.get('title')}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
            if len(unique) >= 5:
                break
        return unique

    def _write_daily_briefing(self, session: BotSession) -> None:
        import json as _json
        from datetime import UTC, datetime

        rows = self._shadow_rows()
        rank = {"BUY": 0, "SELL": 0, "HOLD": 1, "ABANDON": 2}
        ranked = sorted(
            rows,
            key=lambda row: (
                rank.get(str(row.get("action")), 3),
                -float(row.get("strength") or 0),
            ),
        )
        focus = [row for row in ranked if row.get("action") in {"BUY", "SELL"}][:5]
        abandons = [row for row in ranked if row.get("action") == "ABANDON"]
        risks = [
            row for row in ranked
            if row.get("skip_reason") in {"RISK_LIMIT", "DATA_STALE", "MARKET_CLOSED"}
        ]
        intelligence = self._intelligence_rows()
        top_intel = [
            {
                "bot": row.get("bot"),
                "market": row.get("market"),
                "symbol": row.get("symbol"),
                "title": row.get("title"),
                "action": row.get("action"),
                "importance": row.get("importance"),
                "confidence": row.get("confidence"),
                "direction": row.get("direction"),
                "horizon": row.get("horizon"),
            }
            for row in intelligence
            if float(row.get("importance") or 0) >= 0.6
        ][:5]
        audits = self._latest_audits()
        briefing = {
            "as_of": datetime.now(UTC).isoformat(),
            "focus": focus,
            "risks": risks[:8],
            "abandons": abandons[:8],
            "intelligence": top_intel,
            "focus_today": self._focus_today(focus, top_intel, risks),
            "audits": audits[:5],
            "ranked": ranked[:20],
            "counts": {
                "total": len(rows),
                "execute": len(focus),
                "abandon": len(abandons),
                "intelligence": len(intelligence),
            },
        }
        session.memory.remember(
            _json.dumps(briefing, ensure_ascii=False),
            kind="daily-briefing",
            tags=["daily-briefing"],
        )
        session.events.emit(
            "market/chief.briefing", "GLOBAL", "bot", self.name, briefing
        )
        if focus:
            top = "；".join(
                f"{item.get('market')} {item.get('symbol')} {item.get('action')}"
                for item in focus[:3]
            )
            session.memory.remember(
                f"今日优先关注：{top}。仅模拟，不会下单。",
                kind="advice",
                tags=["advice", "briefing"],
            )
        elif top_intel:
            top = "；".join(
                f"{item.get('market')} {item.get('symbol') or ''} {item.get('action')} {item.get('title')}"
                for item in top_intel[:3]
            )
            session.memory.remember(
                f"今日优先情报：{top}。仅 Shadow 建议，不会审批下单。",
                kind="advice",
                tags=["advice", "briefing"],
            )
        elif abandons:
            session.memory.remember(
                f"今日无执行建议，{len(abandons)} 条信号已放弃（闭市/过期/风险）。仅模拟，不会下单。",
                kind="advice",
                tags=["advice", "briefing"],
            )


# 兼容别名：早期命名
MarketChief = MarketChiefAgent
