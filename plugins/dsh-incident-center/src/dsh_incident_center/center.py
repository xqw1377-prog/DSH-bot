"""事故中心 Agent（PRD 设计红线：Kill Switch 独立于 LLM 或单一 DSH 进程）。

职责：
1. 异常检测与分类：接收 incident/opened 事件，按严重度分级
2. 自动降风险：**只接受 risk-policy 签发的结构化 CRITICAL 事件**触发 Kill Switch
3. 通知：所有事故写入记忆与事件流，供复盘
4. 复盘流程：事故 RESOLVED 后生成复盘记录

安全加固（v2）：
- Kill Switch 触发源唯一：risk-policy 服务签发的 risk/limit_breached 事件
  （severity=CRITICAL 且 source="risk-policy"），LLM/Agent 文本判断只能产生
  HIGH 告警，不能自动停盘。
- HALTED 持久化：跨 Incident Center / Gateway 重启保持，避免重启绕过停盘。
- 人工授权恢复：resume_market 必须带审批ID与操作人，审计留痕。
- Kill Switch 失败事件：kill_switch/requested | succeeded | failed，
  退避重试（最多3次：10s/30s/120s），超过上限产生人工告警。
- Kill Switch 失败不产生新 incident/opened（避免重试循环），但产生
  kill_switch/failed 事件并持久化重试状态。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable

from dsh_gateway_client import GatewayClient, GatewayError, RiskPolicyClient, RiskPolicyError
from dsh_runtime import BotSession, KillSwitchStore


class Severity(StrEnum):
    LOW = "LOW"          # 记录即可
    HIGH = "HIGH"        # 需人工介入
    CRITICAL = "CRITICAL"  # 自动 emergency_stop（仅 risk-policy 签发的事件可标记）


# 事故原因关键字到严重度的映射（仅供 incident/opened 文本事件的告警分级，
# 不能用于触发 Kill Switch——Kill Switch 只接受 risk-policy 签发的 CRITICAL）
_SEVERITY_RULES: list[tuple[str, Severity]] = [
    ("emergency", Severity.HIGH),  # 文本里的 emergency 只能告警
    ("kill", Severity.HIGH),
    ("breach", Severity.HIGH),
    ("degraded", Severity.HIGH),
    ("inconsistent", Severity.HIGH),
    ("不一致", Severity.HIGH),
    ("降级", Severity.HIGH),
    ("timeout", Severity.HIGH),
    ("unreachable", Severity.HIGH),
    ("不可达", Severity.HIGH),
]


def classify(reason: str) -> Severity:
    """文本事故的严重度：仅用于告警分级，最高 HIGH（不会返回 CRITICAL）。

    Kill Switch 不能由文本判断触发，必须由 risk-policy 签发的结构化事件触发。
    """
    reason_lower = reason.lower()
    for keyword, sev in _SEVERITY_RULES:
        if keyword.lower() in reason_lower:
            return sev
    return Severity.HIGH  # 未知一律 HIGH，保守告警


@dataclass
class Incident:
    incident_id: str
    market: str
    severity: Severity
    reason: str
    status: str = "OPEN"  # OPEN | MITIGATED | RESOLVED | REVIEWED
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    postmortem: str | None = None
    # 关联的 risk-policy rule_violation_id（若有）
    violation_id: str | None = None


# Kill Switch 退避重试配置：3 次尝试，间隔 10s/30s/120s
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = [timedelta(seconds=10), timedelta(seconds=30), timedelta(seconds=120)]


class IncidentCenter:
    name = "incident-center"

    def __init__(
        self,
        gateway: GatewayClient,
        risk_policy: RiskPolicyClient | None = None,
        kill_switch: Callable[[str, str | None], None] | None = None,
        kill_switch_cooldown_seconds: float = 300.0,
        ks_store: KillSwitchStore | None = None,
    ):
        self.gateway = gateway
        self.risk_policy = risk_policy
        self.gateway_kill = kill_switch  # 测试注入用
        self._incidents: dict[str, Incident] = {}
        self._counter = 0
        # 去抖：market -> 上次 emergency_stop 时间
        self._last_kill: dict[str, datetime] = {}
        self._cooldown = timedelta(seconds=kill_switch_cooldown_seconds)
        # HALTED 状态持久化：若未提供则使用内存 fallback（仅测试场景）
        self._ks_store = ks_store or KillSwitchStore()
        # 内存 fallback（仅当 ks_store 不可用时退化为旧逻辑，生产应总是持久化）
        self._halted_fallback: set[str] = set()

    def tick(self, session: BotSession) -> None:
        """每个 tick：
        1. 扫描文本 incident/opened 事件 → 仅产生 HIGH 告警
        2. 拉取 risk-policy 的 CRITICAL rule-violations → 触发 Kill Switch
        3. 处理到期的退避重试
        """
        session.use("incident_alert")
        self._scan_text_incidents(session)
        self._scan_risk_policy_violations(session)
        self._process_pending_retries(session)

    def _scan_text_incidents(self, session: BotSession) -> None:
        """扫描 incident/opened 文本事件，仅产生告警，不触发 Kill Switch。"""
        try:
            events = session.events.query("incident/opened")
        except Exception:
            return

        for ev in events:
            payload = ev["payload"]
            reason = payload.get("reason", "unknown")
            market = ev["market"]
            if any(i.reason == reason and i.market == market
                   for i in self._incidents.values()):
                continue
            sev = classify(reason)  # 文本最高 HIGH
            self._open(session, ev["event_id"], market, reason, sev, payload, violation_id=None)
            # 文本事件不触发 Kill Switch，仅记录告警
            session.memory.remember(
                f"文本事故 [{sev.value}] {market}: {reason}（仅告警，不自动停盘）",
                kind="incident-alert", tags=[f"market:{market}", "text-only"],
            )

    def _scan_risk_policy_violations(self, session: BotSession) -> None:
        """拉取 risk-policy 的 CRITICAL 事件，触发 Kill Switch。

        这是 Kill Switch 的唯一触发源（设计红线）。
        """
        if self.risk_policy is None:
            return  # 未配置 risk-policy 客户端 → 不能触发 Kill Switch

        try:
            violations = self.risk_policy.list_critical_violations(acknowledged=False)
        except RiskPolicyError as exc:
            session.memory.remember(
                f"risk-policy 不可达，无法拉取 CRITICAL 事件: {exc} — "
                f"这是设计红线，宁可漏触发也不由 LLM/文本判断触发",
                kind="risk-policy-unreachable",
                tags=["kill-switch-source-unavailable"],
            )
            return

        for v in violations:
            self._handle_risk_violation(session, v)

    def _handle_risk_violation(self, session: BotSession, violation: dict) -> None:
        """处理 risk-policy 签发的 CRITICAL 事件：触发 Kill Switch。"""
        # 安全校验：source 必须是 risk-policy，severity 必须 CRITICAL
        if violation.get("source") != "risk-policy":
            session.memory.remember(
                f"拒绝触发 Kill Switch: source != risk-policy "
                f"(got {violation.get('source')})",
                kind="kill-switch-rejected",
                tags=["source-mismatch"],
            )
            return
        if violation.get("severity") != "CRITICAL":
            return  # 非 CRITICAL 不触发

        market = violation["market"]
        violation_id = violation["violation_id"]
        rule_id = violation["rule_id"]

        # 幂等：同 violation_id 已处理过则跳过
        if any(i.violation_id == violation_id for i in self._incidents.values()):
            return

        # 已 HALTED 不重复停
        if self._is_halted(market):
            session.memory.remember(
                f"事故 risk-policy rule={rule_id} {market} CRITICAL，"
                f"但 {market} 已 HALTED，跳过",
                kind="kill-switch-skipped", tags=[f"market:{market}", "halted"],
            )
            return

        # 去抖：冷却期内不重复触发
        if market in self._last_kill:
            elapsed = datetime.now(UTC) - self._last_kill[market]
            if elapsed < self._cooldown:
                session.memory.remember(
                    f"事故 risk-policy rule={rule_id} {market} CRITICAL，"
                    f"但 {market} 在 Kill Switch 冷却期内"
                    f"（剩余 {int((self._cooldown - elapsed).total_seconds())}s），跳过",
                    kind="kill-switch-skipped",
                    tags=[f"market:{market}", "cooldown"],
                )
                return

        # 创建 incident 并触发 Kill Switch
        reason = (
            f"risk-policy CRITICAL: rule={rule_id} "
            f"measured={violation['measured']} > limit={violation['limit']}"
        )
        incident = self._open(
            session, violation_id, market, reason, Severity.CRITICAL,
            violation, violation_id=violation_id,
        )
        self._execute_kill_switch(session, incident, violation)

    def _execute_kill_switch(
        self, session: BotSession, incident: Incident, violation: dict,
    ) -> None:
        """执行 Kill Switch，带退避重试与失败事件。

        失败不产生 incident/opened（避免循环），但产生 kill_switch/failed
        事件并持久化重试状态，下次 tick 自动重试。
        """
        from dsh_contracts import Market

        market = incident.market
        violation_id = incident.violation_id
        account_id = violation.get("account_id")

        # 检查现有 attempt 状态，决定是首次执行还是重试
        last = self._ks_store.last_attempt(incident.incident_id)
        if last and last["status"] in ("SUCCEEDED", "FAILED"):
            # 已有终态 attempt，不重复执行
            return
        if last and last["status"] == "RETRYING":
            # 重试：递增 attempt_no，更新现有记录为 REQUESTED
            attempt_no = last["attempt_no"] + 1
            attempt_id = last["attempt_id"]
            self._ks_store.update_attempt(attempt_id, status="REQUESTED")
        else:
            # 首次：新建 attempt 记录
            attempt_no = 1
            attempt_id = self._ks_store.record_attempt(
                incident_id=incident.incident_id, market=market,
                attempt_no=attempt_no, status="REQUESTED",
                violation_id=violation_id,
            )

        # emit requested 事件
        session.events.emit(
            "kill_switch/requested", market, "bot", self.name,
            {
                "incident_id": incident.incident_id, "market": market,
                "rule_id": violation.get("rule_id", "retry"),
                "source_event_id": violation_id or "",
                "account_id": account_id, "attempt": attempt_no,
            },
        )

        try:
            market_enum = Market(market) if isinstance(market, str) else market
            if self.gateway_kill is not None:
                self.gateway_kill(market, account_id)
            else:
                self.gateway.emergency_stop(market_enum, account_id)

            # 成功：更新 attempt 状态
            self._ks_store.update_attempt(attempt_id, status="SUCCEEDED")
            incident.status = "MITIGATED"
            self._last_kill[market] = datetime.now(UTC)
            self._halt(market, incident.incident_id)
            session.events.emit(
                "kill_switch/succeeded", market, "bot", self.name,
                {
                    "incident_id": incident.incident_id, "market": market,
                    "account_id": account_id, "attempt": attempt_no,
                    "halted": True,
                },
            )
            session.events.emit(
                "incident/mitigated", market, "bot", self.name,
                {
                    "incident_id": incident.incident_id,
                    "action": "emergency_stop_executed",
                    "market": market, "requires_manual_resume": True,
                },
            )
            session.memory.remember(
                f"事故 {incident.incident_id} 触发 Kill Switch 成功（attempt {attempt_no}），"
                f"{market} 已 HALTED（需人工 resume 恢复）",
                kind="mitigation",
                tags=[f"incident:{incident.incident_id}", f"market:{market}", "halted"],
            )
            # 确认 risk-policy 事件，避免重复触发
            if self.risk_policy is not None and violation_id:
                try:
                    self.risk_policy.acknowledge(violation_id)
                except RiskPolicyError as exc:
                    session.memory.remember(
                        f"确认 risk-policy violation {violation_id} 失败: {exc} "
                        f"— 下次 tick 可能重复触发（Kill Switch 已成功，幂等）",
                        kind="acknowledge-failed",
                        tags=[f"violation:{violation_id}"],
                    )
        except (GatewayError, Exception) as exc:
            # 失败：决定是否重试
            will_retry = attempt_no < _MAX_ATTEMPTS
            next_retry_at = None
            if will_retry:
                delay = _RETRY_DELAYS[attempt_no - 1]
                next_retry_at = (datetime.now(UTC) + delay).isoformat()

            self._ks_store.update_attempt(
                attempt_id,
                status="RETRYING" if will_retry else "FAILED",
                last_error=str(exc), next_retry_at=next_retry_at,
            )
            session.events.emit(
                "kill_switch/failed", market, "bot", self.name,
                {
                    "incident_id": incident.incident_id, "market": market,
                    "attempt": attempt_no, "reason": str(exc),
                    "will_retry": will_retry,
                    "next_retry_at": next_retry_at,
                    "requires_human_alert": not will_retry,
                },
            )
            session.memory.remember(
                f"Kill Switch 执行失败（事故 {incident.incident_id}, "
                f"attempt {attempt_no}/{_MAX_ATTEMPTS}, market={market}）: {exc}"
                + (f" — 将在 {next_retry_at} 重试" if will_retry
                   else " — 已达重试上限，需人工立即介入"),
                kind="kill-switch-failed",
                tags=[f"incident:{incident.incident_id}", f"market:{market}",
                      "retry" if will_retry else "human-required"],
            )

    def _process_pending_retries(self, session: BotSession) -> None:
        """处理到期的退避重试。"""
        pending = self._ks_store.pending_retries()
        for p in pending:
            incident = self._incidents.get(p["incident_id"])
            if incident is None:
                continue
            violation = {
                "violation_id": p.get("violation_id"),
                "rule_id": "retry", "market": p["market"],
                "measured": 0, "limit": 0,
                "source": "risk-policy", "severity": "CRITICAL",
                "account_id": None,
            }
            # 把上次 attempt 状态更新为 REQUESTED，重新执行
            self._execute_kill_switch(session, incident, violation)

    # ---- HALTED 持久化 ----

    def _is_halted(self, market: str) -> bool:
        try:
            return self._ks_store.is_halted(market)
        except Exception:
            return market in self._halted_fallback

    def _halt(self, market: str, incident_id: str) -> bool:
        try:
            return self._ks_store.halt(market, incident_id)
        except Exception:
            if market in self._halted_fallback:
                return False
            self._halted_fallback.add(market)
            return True

    def list_halted(self) -> list[dict]:
        """列出当前 HALTED 的市场（跨重启保持）。"""
        try:
            return self._ks_store.list_halted()
        except Exception:
            return [{"market": m, "incident_id": ""} for m in self._halted_fallback]

    def resume_market(
        self, session: BotSession, market: str,
        resumed_by: str, approval_id: str, reason: str,
    ) -> dict | None:
        """人工授权恢复：必须带审批ID与操作人，审计留痕。

        返回 HALTED 记录（含恢复信息），None 表示该 market 未处于 HALTED。
        """
        # 优先用持久化 store
        try:
            record = self._ks_store.resume(market, resumed_by, approval_id, reason)
        except Exception:
            if market not in self._halted_fallback:
                return None
            self._halted_fallback.discard(market)
            record = {
                "market": market, "resumed_by": resumed_by,
                "resume_approval_id": approval_id, "resume_reason": reason,
            }

        if record is None:
            return None

        session.memory.remember(
            f"市场 {market} 经人工授权恢复: 操作人={resumed_by} "
            f"审批ID={approval_id} 原因={reason}",
            kind="manual-resume",
            tags=[f"market:{market}", "human-action", f"approval:{approval_id}"],
        )
        return record

    def resolve(self, session: BotSession, incident_id: str,
                postmortem: str) -> Incident | None:
        """事故复盘：标记 RESOLVED 并记录复盘结论。"""
        incident = self._incidents.get(incident_id)
        if incident is None:
            return None
        incident.status = "RESOLVED"
        incident.resolved_at = datetime.now(UTC)
        incident.postmortem = postmortem
        session.events.emit(
            "incident/resolved", incident.market, "bot", self.name,
            {"incident_id": incident_id, "postmortem": postmortem},
        )
        session.memory.remember(
            f"事故 {incident_id} 已复盘: {postmortem}",
            kind="postmortem", tags=[f"incident:{incident_id}"],
        )
        return incident

    def _open(self, session: BotSession, ev_id: str, market: str,
              reason: str, sev: Severity, payload: dict,
              violation_id: str | None = None) -> Incident:
        self._counter += 1
        incident_id = f"inc-{self._counter:04d}"
        incident = Incident(
            incident_id=incident_id, market=market, severity=sev,
            reason=reason, violation_id=violation_id,
        )
        self._incidents[incident_id] = incident
        session.memory.remember(
            f"事故 {incident_id} 开启 [{sev.value}] {market}: {reason}",
            kind="incident", tags=[f"incident:{incident_id}", f"severity:{sev.value}"],
        )
        return incident
