"""共享交易执行核：审批 → 提交 → 成交 → 严格对账。

Crypto / A 股等专业 Bot 只提供市场、账户和运行模式；
资金动作一律经 Quant Gateway，本核不持有交易密钥。

Shadow 是运行模式：预览后记 SHADOW_RECORDED，禁止 request_order。
"""

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from dsh_contracts import Market, OrderIntent, OrderSide
from dsh_runtime.reconcile import evaluate_reconcile
from dsh_runtime.session import BotSession
from dsh_runtime.ledger import build_entry_plan, build_exit_plan, classify_intel
from dsh_runtime.shadow import build_shadow_decision, simulated_pnl

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def a_share_session_open(now: datetime | None = None) -> bool:
    """A 股交易时段。闭市不得当数据事故。默认用调用方传入的 UTC 时刻。"""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(_SHANGHAI)
    if current.weekday() >= 5:
        return False
    clock = current.time()
    return (time(9, 30) <= clock < time(11, 30)) or (time(13, 0) <= clock < time(15, 0))


def _gateway_status(exc: Exception) -> int | None:
    """识别 Gateway 客户端错误，避免 runtime 硬依赖 gateway-client。"""
    return getattr(exc, "status_code", None)


def _classify_submit_failure(exc: Exception) -> str:
    """按 Gateway 阶段化契约分类。无 phase/submission_unknown 则视为未知。"""
    unknown = getattr(exc, "submission_unknown", None)
    phase = getattr(exc, "phase", None)
    retryable = getattr(exc, "retryable", None)
    error_code = getattr(exc, "error_code", None)
    if error_code == "DUPLICATE_ORDER":
        return "ADOPT"
    if unknown is True:
        return "SUBMISSION_UNKNOWN"
    if unknown is False and phase == "PRE_SUBMIT":
        return "PRE_SUBMIT_BLOCKED" if retryable else "PRE_SUBMIT_FAILED"
    if unknown is False and phase == "VENUE":
        return "ORDER_REJECTED"
    if _gateway_status(exc) == 409:
        return "ADOPT"
    return "SUBMISSION_UNKNOWN"


def _new_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _parse_persisted_valid_until(raw: str | None) -> datetime:
    """解析预览时持久化的 valid_until；缺失时退回「当前+10 分钟」。

    退回值只是兜底：审批绑定里持久化的 valid_until 与下单意图必须一致，
    不一致会被网关以 APPROVAL_INTENT_MISMATCH 拒绝（失败关闭）。
    """
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    return datetime.now(UTC) + timedelta(minutes=10)


def _waiting_confirm(signal: dict) -> bool:
    """6celue 仍在等确认 K 线：这是观望，不是可执行单，也不能当过期作废。"""
    bits = [str(item) for item in (signal.get("why_source") or []) if item]
    for key in ("why", "why_not", "reject_reason", "note"):
        if signal.get(key):
            bits.append(str(signal[key]))
    text = " ".join(bits).lower()
    return "waiting" in text or "confirm" in text or "确认" in text


def _signal_why(signal: dict, *, passed: bool = False) -> str:
    notes = [str(item) for item in (signal.get("why_source") or []) if item]
    head = (
        "正式信号通过风险与市场规则检查"
        if passed
        else f"收到正式信号 {signal.get('symbol')} {signal.get('side')}"
    )
    if notes:
        return f"{head}；源系统说明：{'；'.join(notes[:2])}"
    return head


class TradeExecutionCore:
    UNKNOWN_QUARANTINE_SECONDS = 600.0
    APPROVAL_TTL_MINUTES = 30

    def __init__(
        self,
        *,
        name: str,
        market: Market,
        gateway,
        approvals,
        account_id: str,
        min_strength: float = 0.6,
        mode: str = "paper",
        idempotency_prefix: str = "dsh-paper",
        now_fn: Callable[[], datetime] | None = None,
        policy=None,
    ):
        if mode == "live":
            raise ValueError(
                "live mode is disabled until a real venue adapter, "
                "single-writer store, identity, and outbox are complete"
            )
        if mode not in {"paper", "shadow"}:
            raise ValueError(f"unsupported run mode: {mode}")
        self.name = name
        self.market = market
        self.gateway = gateway
        self.approvals = approvals
        self.account_id = account_id
        self.min_strength = min_strength
        self.mode = mode
        self.idempotency_prefix = idempotency_prefix
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.policy = policy

    def _ledger_fill_id(self, order_id: str, venue_status: dict) -> str | None:
        fills = venue_status.get("fills") or []
        for index, fill in enumerate(fills, start=1):
            fill_id = fill.get("fill_id") or fill.get("id")
            if fill_id:
                return str(fill_id)
            quantity = fill.get("quantity") or fill.get("qty")
            if quantity not in (None, "", "0"):
                return f"{order_id}#{index}"
        filled_quantity = venue_status.get("filled_quantity")
        if filled_quantity not in (None, "", "0"):
            return f"{order_id}#1"
        return None

    def _sync_execution_ledger(
        self,
        session: BotSession,
        signal: dict,
        *,
        task_id: str,
        status: str,
        approval_id: str | None = None,
        order_id: str | None = None,
        fill_id: str | None = None,
        preview: dict | None = None,
        extra_payload: dict | None = None,
    ) -> None:
        task = session.tasks.get(task_id)
        payload = dict((task or {}).get("payload") or signal)
        risk = (preview or {}).get("risk") or {}
        action = str(payload.get("side") or signal.get("side") or "HOLD").upper()
        lane = "SHADOW" if self.mode == "shadow" else "PAPER"
        direction = "POSITIVE" if action == "BUY" else "NEGATIVE" if action == "SELL" else "NEUTRAL"
        entry_price = payload.get("entry_price") or (preview or {}).get("mark_price")
        if entry_price in (None, "") and (preview or {}).get("estimated_cost"):
            try:
                qty = Decimal(str(payload.get("quantity") or "0"))
                if qty > 0:
                    entry_price = str(Decimal(str(preview["estimated_cost"])) / qty)
            except Exception:
                entry_price = None
        ledger_payload = {
            "can_apply": False,
            "live_blocked": True,
            "mode": self.mode,
        }
        if extra_payload:
            ledger_payload.update(extra_payload)
        session.ledger.upsert(
            {
                "market": self.market.value,
                "symbol": payload.get("symbol") or signal.get("symbol"),
                "status": status,
                "intel_grade": "OFFICIAL_PREAUTH",
                "execution_lane": lane,
                "event_id": None,
                "signal_id": payload.get("signal_id") or signal.get("signal_id"),
                "strategy_id": payload.get("strategy_id") or signal.get("strategy_id"),
                "strategy_version": payload.get("strategy_version") or signal.get("strategy_version"),
                "risk_snapshot_id": payload.get("risk_snapshot_id") or f"rs-{signal['signal_id']}",
                "task_id": task_id,
                "approval_id": approval_id or (task or {}).get("approval_id"),
                "order_id": order_id or (task or {}).get("order_id"),
                "fill_id": fill_id,
                "capital_budget": payload.get("risk_budget_delta") or risk.get("risk_budget_delta") or "0",
                "max_risk": payload.get("worst_case_loss") or risk.get("worst_case_loss") or "0",
                "requires_approval": True,
                "action": action,
                "direction": direction,
                "confidence": payload.get("strength"),
                "impact_horizon": "1D",
                "entry_plan": build_entry_plan(
                    action=action,
                    price=entry_price,
                    horizon="15m",
                    max_capital_ratio="0.03",
                ),
                "exit_plan": build_exit_plan(action=action, horizon="1D", event_type="SIGNAL"),
                "evidence_refs": payload.get("evidence_refs")
                or [
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
                "payload": ledger_payload,
            }
        )

    def _transition_event(
        self,
        session: BotSession,
        task_id: str,
        target: str,
        event_type: str,
        payload: dict,
        extra_events: list[dict] | None = None,
        **updates,
    ) -> dict:
        return session.tasks.transition_with_event(
            task_id, target, event_type, self.market.value, "bot", self.name,
            payload, extra_events=extra_events, **updates,
        )

    def tick(self, session: BotSession) -> None:
        self._resume_pending_tasks(session)
        self._process_new_signals(session)
        if self.mode == "shadow":
            self._refresh_shadow_outcomes(session)

    def _resume_pending_tasks(self, session: BotSession) -> None:
        for task in session.tasks.find_by_status("SUBMISSION_UNKNOWN"):
            if self._submission_unknown_timed_out(session, task):
                continue
            self._advance_awaiting(session, task)
        for task in session.tasks.find_by_status("PRE_SUBMIT_BLOCKED"):
            self._retry_pre_submit_blocked(session, task)
        for task in session.tasks.find_by_status(
            "AWAITING_APPROVAL", "APPROVED_SUBMITTING"
        ):
            self._advance_awaiting(session, task)
        for task in session.tasks.find_by_status(
            "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"
        ):
            self._reconcile_fill(session, task)
        for task in session.tasks.find_by_status("FILLED"):
            self._reconcile_account(session, task)

    def _submission_unknown_timed_out(self, session: BotSession, task: dict) -> bool:
        unknown_since = task["payload"].get("submission_unknown_since")
        if not unknown_since:
            payload = dict(task["payload"])
            payload["submission_unknown_since"] = datetime.now(UTC).isoformat()
            session.tasks.update_payload(task["task_id"], payload)
            return False
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(unknown_since)
        ).total_seconds()
        if elapsed <= self.UNKNOWN_QUARANTINE_SECONDS:
            return False
        self._transition_event(
            session, task["task_id"], "INCIDENT",
            "incident/opened",
            {
                "task_id": task["task_id"],
                "reason": "submission UNKNOWN beyond quarantine window",
                "unknown_since": unknown_since,
            },
        )
        session.memory.remember(
            f"任务 {task['task_id']} 提交结果未知超过 "
            f"{self.UNKNOWN_QUARANTINE_SECONDS}s，已开事故，不重新提交",
            kind="error", tags=[task["task_id"], "submission-unknown"],
        )
        return True

    def _advance_awaiting(self, session: BotSession, task: dict) -> None:
        approval_id = task["approval_id"]
        # 已进入提交态的任务不再重新查审批：一次性消费与幂等键由网关裁决，
        # 崩溃/重放时走幂等认领（DUPLICATE_ORDER -> 采纳既有订单），
        # 防止「审批被消费后重放任务卡死」。
        if task["status"] in ("APPROVED_SUBMITTING", "SUBMISSION_UNKNOWN"):
            self._submit(session, task)
            return
        try:
            session.use("query_approvals")
            approval = self.gateway.get_approval(approval_id)
        except Exception as exc:
            status = _gateway_status(exc)
            if status is None:
                raise
            if status == 404:
                self._handle_approval_missing(session, task)
                return
            session.memory.remember(
                f"任务 {task['task_id']} 审批查询失败: {exc}", kind="error",
                tags=[task["task_id"]],
            )
            return

        status = approval.get("status")
        if status == "APPROVED":
            if task["status"] == "AWAITING_APPROVAL":
                problem = self._revalidate(session, task, approval)
                if problem is not None:
                    self._transition_event(
                        session, task["task_id"], "PRE_SUBMIT_FAILED",
                        "order/unknown",
                        {"task_id": task["task_id"], "rejected_at_submit": problem},
                    )
                    self._sync_execution_ledger(
                        session,
                        task["payload"],
                        task_id=task["task_id"],
                        status="PRE_SUBMIT_FAILED",
                        approval_id=approval_id,
                        extra_payload={"rejected_at_submit": problem},
                    )
                    session.memory.remember(
                        f"任务 {task['task_id']} 执行前校验失败（不下单）: {problem}",
                        kind="error", tags=[task["task_id"], "revalidate-failed"],
                    )
                    return
                session.tasks.transition(task["task_id"], "APPROVED_SUBMITTING")
                task = session.tasks.get(task["task_id"])
                self._sync_execution_ledger(
                    session,
                    task["payload"],
                    task_id=task["task_id"],
                    status="APPROVED_SUBMITTING",
                    approval_id=approval_id,
                )
            self._submit(session, task)
        elif status == "REJECTED":
            self._transition_event(
                session, task["task_id"], "REJECTED",
                "approval/rejected",
                {"approval_id": approval_id, "task_id": task["task_id"]},
            )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="REJECTED",
                approval_id=approval_id,
                extra_payload={"approval_status": "REJECTED"},
            )
        elif status == "EXPIRED":
            self._transition_event(
                session, task["task_id"], "EXPIRED",
                "approval/rejected",
                {"approval_id": approval_id, "task_id": task["task_id"],
                 "reason": "approval expired"},
            )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="EXPIRED",
                approval_id=approval_id,
                extra_payload={"approval_status": "EXPIRED"},
            )

    def _handle_approval_missing(self, session: BotSession, task: dict) -> None:
        approval_id = task["approval_id"]
        requested_at = task["payload"].get("approval_requested_at")
        locally_expired = bool(requested_at) and (
            datetime.now(UTC) - datetime.fromisoformat(requested_at)
        ) > timedelta(minutes=self.APPROVAL_TTL_MINUTES)

        audited = self._approval_creation_audited(approval_id)

        if locally_expired and audited:
            self._transition_event(
                session, task["task_id"], "EXPIRED",
                "approval/rejected",
                {"approval_id": approval_id, "task_id": task["task_id"],
                 "reason": "approval expired and purged (locally confirmed)"},
            )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="EXPIRED",
                approval_id=approval_id,
                extra_payload={"approval_status": "EXPIRED_PURGED"},
            )
            return

        self._transition_event(
            session, task["task_id"], "APPROVAL_UNKNOWN",
            "incident/opened",
            {"approval_id": approval_id, "task_id": task["task_id"],
             "reason": "approval 404 but not locally confirmed expired",
             "locally_expired": locally_expired, "creation_audited": audited},
        )
        self._sync_execution_ledger(
            session,
            task["payload"],
            task_id=task["task_id"],
            status="APPROVAL_UNKNOWN",
            approval_id=approval_id,
            extra_payload={
                "approval_status": "UNKNOWN",
                "locally_expired": locally_expired,
                "creation_audited": audited,
            },
        )
        session.memory.remember(
            f"审批 {approval_id} 查询 404 但无法本地确认过期"
            f"（本地过期={locally_expired}，审计存在={audited}），"
            f"任务 APPROVAL_UNKNOWN 失败关闭，需人工核查账本",
            kind="error", tags=[task["task_id"], "approval-unknown"],
        )

    def _approval_creation_audited(self, approval_id: str) -> bool:
        try:
            audit = self.gateway._client.get("/v1/audit?limit=1000").json()
        except Exception:
            return False
        return any(
            e.get("action") == "approval.requested"
            and f"approval_id={approval_id}" in (e.get("detail") or "")
            for e in audit
        )

    def _revalidate(self, session: BotSession, task: dict,
                     approval: dict) -> str | None:
        signal = task["payload"]
        signal_id = signal["signal_id"]

        expected_refs = {
            f"signal:{signal_id}",
            f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
        }
        if not expected_refs.issubset(set(approval.get("evidence_refs", []))):
            return (f"approval evidence mismatch: expected {sorted(expected_refs)}, "
                    f"got {approval.get('evidence_refs')}")

        valid_until = signal.get("valid_until")
        if valid_until:
            until = datetime.fromisoformat(valid_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if self.now_fn() > until:
                return f"signal expired at {valid_until}"
        try:
            session.use("query_signals")
            current = self.gateway.get_signals(self.market)
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            return f"cannot re-read signals: {exc}"
        live = next((s for s in current if s["signal_id"] == signal_id), None)
        if live is None:
            return "signal no longer active"
        if (live["side"] != signal["side"]
                or live["strategy_version"] != signal["strategy_version"]):
            return (f"signal changed: side {signal['side']}->{live['side']}, "
                    f"version {signal['strategy_version']}->{live['strategy_version']}")

        try:
            fresh = self.gateway.preview_order(OrderIntent(
                idempotency_key=f"reval-{signal_id}",
                market=self.market,
                account_id=self.account_id,
                strategy_id=signal["strategy_id"],
                strategy_version=signal["strategy_version"],
                symbol=signal["symbol"],
                side=OrderSide(signal["side"]),
                quantity=signal.get("quantity", "0.01"),
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                signal_snapshot_id=signal_id,
                risk_snapshot_id=signal["risk_snapshot_id"],
                order_type="MARKET",
            ))
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            return f"cannot re-preview: {exc}"
        fresh_risk = fresh.get("risk", {})
        boundary = signal.get("risk_boundary") or {}
        if not boundary:
            return "task payload missing approved risk boundary"
        ceiling_checks = [
            ("worst_case_loss", fresh_risk.get("worst_case_loss"),
             boundary.get("max_worst_case_loss")),
            ("notional", fresh_risk.get("risk_budget_delta"),
             boundary.get("max_notional")),
            ("slippage", fresh.get("estimated_slippage"),
             boundary.get("max_slippage")),
        ]
        for name, fresh_value, ceiling in ceiling_checks:
            if fresh_value is None or ceiling is None:
                return f"risk boundary incomplete: {name}"
            try:
                exceeded = Decimal(str(fresh_value)) > Decimal(str(ceiling))
            except Exception:
                return f"risk boundary not numeric: {name}"
            if exceeded:
                return (f"risk exceeds approved boundary: {name} "
                        f"{fresh_value} > {ceiling}")
        try:
            if Decimal(str(fresh_risk.get("position_after", "0"))) > Decimal(
                    boundary.get("max_quantity", "0")):
                return (f"position_after exceeds approved max_quantity "
                        f"{boundary.get('max_quantity')}")
        except Exception:
            return "risk boundary max_quantity not numeric"
        return None

    def _submit(self, session: BotSession, task: dict) -> None:
        signal = task["payload"]
        persisted = signal.get("order_intent")
        if persisted:
            intent = OrderIntent.model_validate(persisted)
            self._dispatch_submit(session, task, intent)
            return
        valid_until = _parse_persisted_valid_until(signal.get("valid_until"))
        intent = OrderIntent(
            idempotency_key=task["idempotency_key"],
            market=self.market,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=signal.get("quantity", "0.01"),
            valid_until=valid_until,
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id=signal["risk_snapshot_id"],
            approval_id=task["approval_id"],
            order_type="MARKET",
        )
        payload = dict(signal)
        payload["order_intent"] = intent.model_dump(mode="json")
        session.tasks.update_payload(task["task_id"], payload)
        self._dispatch_submit(session, task, intent)

    def _dispatch_submit(self, session: BotSession, task: dict,
                         intent: OrderIntent) -> None:
        # 风险快照由网关在 _preview 时按权威持仓/价格签发并持久化
        # （intent.risk_snapshot_id 即预览返回的快照 ID）；
        # Bot 不再自报快照，提交时由网关校验归属/绑定/新鲜度。
        signal = task["payload"]
        try:
            session.use("submit_order")
        except Exception as exc:
            if _gateway_status(exc) is None and getattr(exc, "submission_unknown", None) is None:
                raise
            self._mark_submit_outcome(session, task, "PRE_SUBMIT_FAILED", exc)
            return
        try:
            result = self.gateway.request_order(intent)
        except Exception as exc:
            if _gateway_status(exc) is None and getattr(exc, "submission_unknown", None) is None:
                raise
            outcome = _classify_submit_failure(exc)
            if outcome == "ADOPT":
                self._adopt_existing_order(session, task)
                return
            self._mark_submit_outcome(session, task, outcome, exc)
            return

        self._transition_event(
            session, task["task_id"], "SUBMITTED",
            "order/submitted",
            {
                "order_id": result["order_id"],
                "idempotency_key": intent.idempotency_key,
                "market": self.market.value,
                "approval_id": task["approval_id"],
                "submitted_at": datetime.now(UTC).isoformat(),
            },
            order_id=result["order_id"],
        )
        self._sync_execution_ledger(
            session,
            signal,
            task_id=task["task_id"],
            status="SUBMITTED",
            approval_id=task["approval_id"],
            order_id=result["order_id"],
            extra_payload={
                "submitted_at": datetime.now(UTC).isoformat(),
                "idempotency_key": intent.idempotency_key,
            },
        )
        session.memory.remember(
            f"订单 {result['order_id']} 已提交，审批 {task['approval_id']}",
            kind="order-submitted", tags=[task["task_id"], result["order_id"]],
        )
        submitted = session.tasks.get(task["task_id"])
        if submitted is not None:
            self._reconcile_fill(session, submitted)

    def _mark_submit_outcome(
        self, session: BotSession, task: dict, outcome: str, exc: Exception
    ) -> None:
        current = session.tasks.get(task["task_id"]) or task
        payload = dict(current["payload"])
        payload["submit_error"] = str(exc)
        payload["submit_error_code"] = getattr(exc, "error_code", None)
        payload["submit_phase"] = getattr(exc, "phase", None)
        if outcome == "SUBMISSION_UNKNOWN":
            payload.setdefault("submission_unknown_since", datetime.now(UTC).isoformat())
        from dsh_runtime.store import transaction

        with transaction():
            session.tasks.update_payload(task["task_id"], payload)
            if current["status"] != outcome:
                if outcome == "SUBMISSION_UNKNOWN":
                    self._transition_event(
                        session, task["task_id"], outcome,
                        "order/unknown",
                        {"task_id": task["task_id"],
                         "idempotency_key": task["idempotency_key"],
                         "error": str(exc)},
                    )
                else:
                    session.tasks.transition(task["task_id"], outcome)
        self._sync_execution_ledger(
            session,
            current["payload"],
            task_id=task["task_id"],
            status=outcome,
            approval_id=current.get("approval_id"),
            order_id=current.get("order_id"),
            extra_payload={
                "submit_error": str(exc),
                "submit_error_code": getattr(exc, "error_code", None),
                "submit_phase": getattr(exc, "phase", None),
            },
        )
        session.memory.remember(
            f"任务 {task['task_id']} 提交结果 {outcome}: {exc}",
            kind="error", tags=[task["task_id"], "submit-failed", outcome],
        )

    def _retry_pre_submit_blocked(self, session: BotSession, task: dict) -> None:
        approval_id = task.get("approval_id")
        if not approval_id:
            session.tasks.transition(task["task_id"], "PRE_SUBMIT_FAILED")
            return
        try:
            approval = self.gateway.get_approval(approval_id)
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            return
        if approval.get("status") != "APPROVED":
            session.tasks.transition(task["task_id"], "PRE_SUBMIT_FAILED")
            return
        problem = self._revalidate(session, task, approval)
        if problem is not None:
            session.tasks.transition(task["task_id"], "PRE_SUBMIT_FAILED")
            session.memory.remember(
                f"任务 {task['task_id']} 阻塞后重评失败: {problem}",
                kind="error", tags=[task["task_id"]],
            )
            return
        session.tasks.transition(task["task_id"], "APPROVED_SUBMITTING")
        self._submit(session, session.tasks.get(task["task_id"]))

    def _adopt_existing_order(self, session: BotSession, task: dict) -> None:
        resp = self.gateway._client.get(
            f"/v1/idempotency-keys/{task['idempotency_key']}"
        )
        order_id = resp.json().get("order_id") if resp.status_code == 200 else None
        if not order_id:
            current = session.tasks.get(task["task_id"]) or task
            payload = dict(current["payload"])
            payload.setdefault("submission_unknown_since", datetime.now(UTC).isoformat())
            session.tasks.update_payload(task["task_id"], payload)
            if current["status"] != "SUBMISSION_UNKNOWN":
                session.tasks.transition(task["task_id"], "SUBMISSION_UNKNOWN")
            session.memory.remember(
                f"任务 {task['task_id']} 幂等键占用但暂无订单（在途/恢复窗口），"
                f"保持查询恢复",
                kind="order-unknown", tags=[task["task_id"]],
            )
            return
        session.tasks.transition(task["task_id"], "SUBMITTED", order_id=order_id)
        self._sync_execution_ledger(
            session,
            task["payload"],
            task_id=task["task_id"],
            status="SUBMITTED",
            approval_id=task.get("approval_id"),
            order_id=order_id,
            extra_payload={"adopted_existing_order": True},
        )
        session.memory.remember(
            f"任务 {task['task_id']} 认领既有订单 {order_id}（幂等冲突，不重复下单）",
            kind="order-adopted", tags=[task["task_id"], order_id],
        )
        self._reconcile_fill(session, session.tasks.get(task["task_id"]))

    def _reconcile_fill(self, session: BotSession, task: dict) -> None:
        order_id = task.get("order_id")
        if not order_id:
            return
        try:
            session.use("query_order_status")
            status = self.gateway.get_order_status(self.market, order_id)
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            session.memory.remember(
                f"订单 {order_id} 状态查询失败: {exc}", kind="error",
                tags=[task["task_id"], order_id],
            )
            return

        order_status = status.get("status")
        if order_status == "CANCELLED":
            self._transition_event(
                session, task["task_id"], "CANCELLED",
                "order/cancelled",
                {"task_id": task["task_id"], "order_id": order_id,
                 "market": self.market.value,
                 "cancelled_at": datetime.now(UTC).isoformat()},
            )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="CANCELLED",
                approval_id=task.get("approval_id"),
                order_id=order_id,
            )
            return
        if order_status == "REJECTED":
            self._transition_event(
                session, task["task_id"], "ORDER_REJECTED",
                "order/unknown",
                {"task_id": task["task_id"], "order_id": order_id,
                 "reason": "order rejected by venue"},
            )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="ORDER_REJECTED",
                approval_id=task.get("approval_id"),
                order_id=order_id,
                extra_payload={"venue_status": "REJECTED"},
            )
            return
        if order_status == "PARTIALLY_FILLED":
            fill_id = self._ledger_fill_id(order_id, status)
            fill_payload = {
                "task_id": task["task_id"], "order_id": order_id,
                "market": self.market.value,
                "symbol": status.get("symbol")
                          or task["payload"].get("symbol"),
                "partial": True,
                "filled_quantity": str(status.get("filled_quantity") or "0"),
                "avg_price": str(status.get("avg_price") or "0"),
                "filled_at": status.get("filled_at")
                             or datetime.now(UTC).isoformat(),
            }
            if task["status"] != "PARTIALLY_FILLED":
                self._transition_event(
                    session, task["task_id"], "PARTIALLY_FILLED",
                    "order/filled", fill_payload,
                )
            else:
                session.events.emit(
                    "order/filled", self.market.value, "bot", self.name,
                    fill_payload,
                )
            self._sync_execution_ledger(
                session,
                task["payload"],
                task_id=task["task_id"],
                status="PARTIALLY_FILLED",
                approval_id=task.get("approval_id"),
                order_id=order_id,
                fill_id=fill_id,
                extra_payload={"venue_fill": fill_payload},
            )
            return
        if order_status == "UNKNOWN":
            unknown_since = task["payload"].get("unknown_since")
            if not unknown_since:
                payload = dict(task["payload"])
                payload["unknown_since"] = datetime.now(UTC).isoformat()
                session.tasks.update_payload(task["task_id"], payload)
                session.memory.remember(
                    f"订单 {order_id} 状态 UNKNOWN，进入隔离查询（不重新提交）",
                    kind="order-unknown", tags=[task["task_id"], order_id],
                )
                self._sync_execution_ledger(
                    session,
                    task["payload"],
                    task_id=task["task_id"],
                    status="SUBMISSION_UNKNOWN",
                    approval_id=task.get("approval_id"),
                    order_id=order_id,
                    extra_payload={"venue_status": "UNKNOWN"},
                )
                return
            elapsed = (datetime.now(UTC)
                       - datetime.fromisoformat(unknown_since)).total_seconds()
            if elapsed > self.UNKNOWN_QUARANTINE_SECONDS:
                self._transition_event(
                    session, task["task_id"], "INCIDENT",
                    "incident/opened",
                    {"task_id": task["task_id"], "order_id": order_id,
                     "reason": "order UNKNOWN beyond quarantine window",
                     "unknown_since": unknown_since},
                )
                session.memory.remember(
                    f"订单 {order_id} UNKNOWN 超过 {self.UNKNOWN_QUARANTINE_SECONDS}s，"
                    f"已开事故等待人工处理",
                    kind="error", tags=[task["task_id"], order_id],
                )
                self._sync_execution_ledger(
                    session,
                    task["payload"],
                    task_id=task["task_id"],
                    status="INCIDENT",
                    approval_id=task.get("approval_id"),
                    order_id=order_id,
                    extra_payload={"venue_status": "UNKNOWN_TIMEOUT", "unknown_since": unknown_since},
                )
            return
        if order_status != "FILLED":
            if order_status == "ACKNOWLEDGED" and task["status"] == "SUBMITTED":
                session.tasks.transition(task["task_id"], "ACKNOWLEDGED")
                self._sync_execution_ledger(
                    session,
                    task["payload"],
                    task_id=task["task_id"],
                    status="ACKNOWLEDGED",
                    approval_id=task.get("approval_id"),
                    order_id=order_id,
                )
            return

        fill_id = self._ledger_fill_id(order_id, status)
        fill_payload = {
            "order_id": order_id,
            "market": self.market.value,
            "symbol": status.get("symbol") or task["payload"].get("symbol"),
            "filled_quantity": str(
                status.get("filled_quantity")
                or task["payload"].get("quantity")
                or "0"
            ),
            "avg_price": str(status.get("avg_price") or "0"),
            "filled_at": status.get("filled_at") or datetime.now(UTC).isoformat(),
            "fees": str(status.get("fees") or "0"),
            "approval_id": task.get("approval_id"),
            "task_id": task["task_id"],
        }
        if task["status"] != "FILLED":
            self._transition_event(
                session, task["task_id"], "FILLED",
                "order/filled", fill_payload,
            )
        else:
            session.events.emit(
                "order/filled", self.market.value, "bot", self.name,
                fill_payload,
            )
        self._sync_execution_ledger(
            session,
            task["payload"],
            task_id=task["task_id"],
            status="FILLED",
            approval_id=task.get("approval_id"),
            order_id=order_id,
            fill_id=fill_id,
            extra_payload={"venue_fill": fill_payload},
        )
        session.memory.remember(
            f"订单 {order_id} 已成交",
            kind="order-filled", tags=[task["task_id"], order_id],
        )
        self._reconcile_account(session, session.tasks.get(task["task_id"]))

    def _reconcile_account(self, session: BotSession, task: dict) -> None:
        if task is None:
            return
        order_id = task["order_id"]
        payload = task["payload"]
        baseline = payload.get("reconcile_baseline") or {}
        symbol = payload["symbol"]
        side = baseline.get("side") or payload.get("side") or "BUY"
        try:
            session.use("query_order_status")
            venue_order = self.gateway.get_order_status(self.market, order_id)
            session.use("query_positions")
            positions = self.gateway.get_positions(
                self.market, account_id=self.account_id
            )
            session.use("query_accounts")
            accounts = self.gateway.get_account_summary(self.market)
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            session.tasks.transition(task["task_id"], "FILLED",
                                     reconciliation_status="PENDING")
            session.memory.remember(
                f"订单 {order_id} 对账数据获取失败（保持 FILLED 待重试）: {exc}",
                kind="error", tags=[task["task_id"]],
            )
            return
        match = next((p for p in positions if p.get("symbol") == symbol), None)
        account = next(
            (a for a in accounts if a.get("account_id") == self.account_id),
            accounts[0] if accounts else None,
        )
        verdict = evaluate_reconcile(
            side=side,
            baseline_position=baseline.get("position_quantity", "0"),
            baseline_cash=baseline.get("cash", "0"),
            venue=venue_order,
            position=match,
            account=account,
        )
        reconciliation = {
            "task_id": task["task_id"],
            "order_id": order_id,
            "expected_quantity": verdict.details.get("filled_quantity", "0"),
            "execution_status": "FILLED",
            "symbol": symbol,
            "reconciled_at": datetime.now(UTC).isoformat(),
            "venue_as_of": str(
                venue_order.get("filled_at") or venue_order.get("as_of")
            ),
            **verdict.details,
        }
        if match is not None:
            reconciliation.update(
                {
                    "positions_quantity": str(match.get("quantity")),
                    "available_quantity": str(match.get("available_quantity")),
                    "frozen_quantity": str(match.get("frozen_quantity", "0")),
                }
            )
        if account is not None:
            reconciliation.update(
                {
                    "cash": str(account.get("cash")),
                    "equity": str(account.get("equity")),
                    "reconciliation_version": account.get("reconciliation_version"),
                }
            )
        if not verdict.matched:
            reconciliation["reconciliation_status"] = "MISMATCH"
            reconciliation["reason"] = "; ".join(verdict.reasons)
            mismatch = {k: reconciliation[k] for k in (
                "order_id", "task_id", "expected_quantity",
                "positions_quantity", "detail", "execution_status",
                "reconciliation_status", "numeric_problems", "reason",
            ) if k in reconciliation}
            self._transition_event(
                session, task["task_id"], "INCIDENT",
                "account/mismatch", mismatch,
                extra_events=[{
                    "event_type": "incident/opened",
                    "market": self.market.value,
                    "actor_kind": "bot",
                    "actor_id": self.name,
                    "payload": {
                        "task_id": task["task_id"],
                        "order_id": order_id,
                        "reason": reconciliation["reason"],
                    },
                }],
                reconciliation_status="MISMATCH",
            )
            self._sync_execution_ledger(
                session,
                payload,
                task_id=task["task_id"],
                status="INCIDENT",
                approval_id=task.get("approval_id"),
                order_id=order_id,
                fill_id=self._ledger_fill_id(order_id, venue_order),
                extra_payload={"reconciliation": reconciliation},
            )
            session.memory.remember(
                f"对账异常：订单 {order_id} {reconciliation['reason']}",
                kind="error", tags=[task["task_id"], "reconcile-mismatch"],
            )
            return
        reconciliation["reconciliation_status"] = "MATCHED"
        self._transition_event(
            session, task["task_id"], "DONE",
            "account/reconciled",
            {k: reconciliation[k] for k in (
                "order_id", "task_id", "execution_status",
                "reconciliation_status", "symbol", "quantity",
                "filled_quantity", "avg_price", "fees",
                "positions_quantity", "available_quantity",
                "frozen_quantity", "cash", "equity",
                "reconciliation_version", "reconciled_at", "venue_as_of",
            ) if k in reconciliation},
            reconciliation_status="MATCHED",
        )
        self._sync_execution_ledger(
            session,
            payload,
            task_id=task["task_id"],
            status="RECONCILED",
            approval_id=task.get("approval_id"),
            order_id=order_id,
            fill_id=self._ledger_fill_id(order_id, venue_order),
            extra_payload={"reconciliation": reconciliation},
        )
        session.memory.remember(
            f"订单 {order_id} 对账通过（{symbol} {verdict.details.get('filled_quantity')}）",
            kind="order-reconciled", tags=[task["task_id"], order_id],
        )

    def _process_new_signals(self, session: BotSession) -> None:
        session.use("query_health")
        health = self.gateway.get_health(self.market)
        closed = self._ashare_session_closed(health)
        stale = not health.get("system_ok") or not health.get("data_fresh")
        if closed:
            session.memory.remember(
                "A股闭市，跳过信号处理，不记数据事故",
                kind="market-closed",
                tags=["market-closed"],
            )
            if self.mode != "shadow":
                return
        elif stale:
            session.events.emit(
                "incident/opened", self.market.value, "bot", self.name,
                {"reason": "market data degraded"},
            )
            session.memory.remember(
                "交易所/数据状态降级，本 tick 跳过信号处理", kind="incident",
                tags=["degraded"],
            )
            if self.mode != "shadow":
                return

        session.use("query_signals")
        for raw in self.gateway.get_signals(self.market):
            signal = raw if isinstance(raw, dict) else raw.model_dump(mode="json")
            self._process_signal(session, signal, closed=closed, stale=stale and not closed)

    def _ashare_session_closed(self, health: dict) -> bool:
        if self.market != Market.A_SHARE:
            return False
        declared = str(health.get("market_session") or "").upper()
        if declared == "CLOSED":
            return True
        if declared == "OPEN":
            return False
        return not a_share_session_open(self.now_fn())

    def _signal_quantity(self, signal: dict) -> str:
        if signal.get("quantity") not in (None, ""):
            return str(signal["quantity"])
        if self.policy is not None:
            return str(self.policy.default_quantity(signal))
        if self.market == Market.A_SHARE:
            # A 股缺省数量必须是整手，Paper 同样强制（不允许 0.01 股）
            return "100"
        return "0.01"

    def _gate_reason(self, signal: dict, *, closed: bool, stale: bool) -> tuple[str, str] | None:
        if closed:
            return "MARKET_CLOSED", "A股闭市，不执行"
        if stale:
            return "DATA_STALE", "数据过期或系统降级，失败关闭"
        if _waiting_confirm(signal):
            return None
        valid_until = signal.get("valid_until")
        if valid_until:
            until = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if self.now_fn() > until:
                return "SIGNAL_EXPIRED", f"信号已过期（{valid_until}）"
        strength = float(signal.get("strength") or 0.0)
        if strength < self.min_strength:
            return "LOW_STRENGTH", f"强度 {strength} 低于阈值 {self.min_strength}"
        if self.policy is not None:
            blocked = self.policy.session_blocked(self.now_fn())
            if blocked:
                return "MARKET_CLOSED", str(blocked)
        return None

    def _policy_risk_block(self, session: BotSession, signal: dict, preview: dict) -> str | None:
        risk = preview.get("risk") or {}
        limits = risk.get("limits_hit") or []
        if limits:
            return f"风险超限: {', '.join(str(item) for item in limits)}"
        # 市场规则（整手/涨跌停/T+1）在 Shadow 与 Paper 同样强制：
        # Shadow 记录「若执行将被拒绝」，Paper 必须真实拒绝——
        # Paper 放行 0.01 股或非整手订单等于用假数据训练进化链路。
        quantity = self._signal_quantity(signal)
        # validate_order 需要每股价格：estimated_cost 是订单总金额，
        # 回退时按数量折算，避免把名义价值当单价做涨跌停校验
        price = signal.get("entry_price")
        if price in (None, ""):
            cost = preview.get("estimated_cost")
            try:
                qty_dec = Decimal(str(quantity))
                price = (
                    str(Decimal(str(cost)) / qty_dec)
                    if cost not in (None, "") and qty_dec > 0 else cost
                )
            except Exception:
                price = cost
        if self.policy is not None:
            problem = self.policy.validate_order(
                self.market, quantity, price, signal.get("symbol")
            )
            if problem:
                return problem
        if self.market != Market.A_SHARE:
            return None
        qty = Decimal(str(quantity))
        if qty % Decimal("100") != 0:
            return "A 股必须整手（100 股）"
        if str(signal.get("side") or "").upper() != "SELL":
            return None
        try:
            session.use("query_positions")
            positions = self.gateway.get_positions(
                self.market, account_id=self.account_id
            )
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            return None
        match = next(
            (row for row in positions if row.get("symbol") == signal.get("symbol")),
            None,
        )
        if match is None:
            return "T+1 无可用持仓，不能卖出"
        available = Decimal(str(match.get("available_quantity") or "0"))
        if available < qty:
            return "T+1 可用不足，当日买入不可卖"
        return None

    def _process_signal(
        self,
        session: BotSession,
        signal: dict,
        *,
        closed: bool = False,
        stale: bool = False,
    ) -> None:
        signal_id = signal["signal_id"]
        if session.memory.has_tagged(f"signal:{signal_id}"):
            return

        strength = float(signal.get("strength") or 0.0)
        if (
            self.mode == "shadow"
            and _waiting_confirm(signal)
            and not closed
            and not stale
        ):
            self._record_shadow(
                session,
                signal,
                action="WATCH",
                skip_reason="WAITING_CONFIRM",
                why=_signal_why(signal),
                why_not="源系统还在等确认 K 线，这不是已经可以下的单",
                preview=None,
            )
            return
        gated = self._gate_reason(signal, closed=closed, stale=stale)
        if gated is not None:
            reason, why_not = gated
            if self.mode == "shadow":
                self._record_shadow(
                    session,
                    signal,
                    action="ABANDON",
                    skip_reason=reason,
                    why=_signal_why(signal),
                    why_not=why_not,
                    preview=None,
                )
            else:
                session.memory.remember(
                    f"信号 {signal_id} 不执行：{why_not}",
                    kind="signal-skip",
                    tags=[f"signal:{signal_id}"],
                )
            return

        task_id = session.tasks.create(
            kind="shadow-decision" if self.mode == "shadow" else "paper-order",
            subject_id=signal_id,
            payload=signal,
        )
        task = session.tasks.get(task_id)
        assert task is not None
        if task["status"] == "SHADOW_RECORDED":
            session.memory.remember(
                f"Shadow 决策已存在 {signal_id}，幂等跳过",
                kind="shadow-decision",
                tags=[f"signal:{signal_id}", task_id],
            )
            return

        preview = self._preview(session, task_id, signal)
        if preview is None:
            if self.mode == "shadow":
                self._finish_shadow(
                    session,
                    task_id,
                    signal,
                    action="ABANDON",
                    skip_reason="PREVIEW_FAILED",
                    why=_signal_why(signal),
                    why_not="订单预览失败，失败关闭",
                    preview=None,
                )
            return

        blocked = self._policy_risk_block(session, signal, preview)
        if blocked:
            if self.mode == "shadow":
                self._finish_shadow(
                    session,
                    task_id,
                    signal,
                    action="ABANDON",
                    skip_reason="RISK_LIMIT",
                    why=_signal_why(signal),
                    why_not=blocked,
                    preview=preview,
                )
            else:
                session.tasks.transition(task_id, "PRE_SUBMIT_FAILED")
                session.memory.remember(
                    f"信号 {signal_id} 风险/规则拦截: {blocked}",
                    kind="signal-skip",
                    tags=[f"signal:{signal_id}"],
                )
            return

        if self.mode == "shadow":
            self._finish_shadow(
                session,
                task_id,
                signal,
                action=str(signal.get("side") or "HOLD"),
                skip_reason=None,
                why=_signal_why(signal, passed=True),
                why_not="无；仅模拟，不会下单",
                preview=preview,
            )
            return

        approval_id = self._request_approval(session, task_id, signal)
        if approval_id is None:
            return

        self._transition_event(
            session, task_id, "AWAITING_APPROVAL",
            "approval/requested",
            {
                "approval_id": approval_id,
                "market": self.market.value,
                "requested_by_bot": self.name,
                "subject_type": "order",
                "subject_id": signal["signal_id"],
                "requested_at": datetime.now(UTC).isoformat(),
                "task_id": task_id,
                "evidence_refs": [
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
            },
            approval_id=approval_id,
        )
        session.memory.remember(
            f"信号 {signal_id}（{signal['symbol']} {signal['side']} "
            f"强度 {strength}）已生成订单预览并等待人工审批 {approval_id}",
            kind="signal-processed", tags=[f"signal:{signal_id}", task_id],
        )

    def _record_shadow(
        self,
        session: BotSession,
        signal: dict,
        *,
        action: str,
        skip_reason: str | None,
        why: str,
        why_not: str,
        preview: dict | None,
    ) -> None:
        task_id = session.tasks.create(
            kind="shadow-decision",
            subject_id=signal["signal_id"],
            payload=signal,
        )
        self._finish_shadow(
            session,
            task_id,
            signal,
            action=action,
            skip_reason=skip_reason,
            why=why,
            why_not=why_not,
            preview=preview,
        )

    def _finish_shadow(
        self,
        session: BotSession,
        task_id: str,
        signal: dict,
        *,
        action: str,
        skip_reason: str | None,
        why: str,
        why_not: str,
        preview: dict | None,
    ) -> None:
        task = session.tasks.get(task_id)
        assert task is not None
        if task["status"] == "SHADOW_RECORDED":
            session.memory.remember(
                f"Shadow 模式记录决策 {signal['signal_id']}，不下单",
                kind="shadow-decision",
                tags=[f"signal:{signal['signal_id']}", task_id],
            )
            return
        risk = (preview or {}).get("risk") or {}
        suggested = (
            signal.get("entry_price")
            or (preview or {}).get("mark_price")
        )
        if suggested in (None, "") and preview and preview.get("estimated_cost"):
            qty = Decimal(self._signal_quantity(signal))
            if qty > 0:
                suggested = str(Decimal(str(preview["estimated_cost"])) / qty)
        payload = dict(task["payload"])
        payload["shadow_decision"] = build_shadow_decision(
            signal,
            action=action,
            quantity=self._signal_quantity(signal),
            suggested_price=None if suggested in (None, "") else str(suggested),
            strategy_version=str(signal.get("strategy_version") or ""),
            strength=float(signal.get("strength") or 0.0),
            position_after=str(risk.get("position_after") or "0"),
            worst_case_loss=str(risk.get("worst_case_loss") or "0"),
            primary_risks=[skip_reason] if skip_reason else list(risk.get("limits_hit") or []),
            why=why,
            why_not=why_not,
            skip_reason=skip_reason,
            evidence_refs=signal.get("evidence_refs"),
            valid_until=str(signal.get("valid_until") or ""),
        )
        session.tasks.update_payload(task_id, payload)
        if task["status"] in {"SIGNAL_RECEIVED", "PREVIEWED"}:
            session.tasks.transition(task_id, "SHADOW_RECORDED")
        session.memory.remember(
            f"Shadow 模式记录决策 {signal['signal_id']}，不下单",
            kind="shadow-decision",
            tags=[f"signal:{signal['signal_id']}", task_id],
        )
        grade, lane, needs_approval = classify_intel(
            authority="official",
            source_tier="PRIMARY",
            action=action,
            held=False,
            title=str(signal.get("symbol") or ""),
            event_type="SIGNAL",
            importance=float(signal.get("strength") or 0.0),
        )
        session.ledger.upsert(
            {
                "market": self.market.value,
                "symbol": signal.get("symbol"),
                "status": "SHADOW",
                "intel_grade": grade,
                "execution_lane": lane,
                "event_id": None,
                "signal_id": signal.get("signal_id"),
                "strategy_id": signal.get("strategy_id"),
                "strategy_version": signal.get("strategy_version"),
                "risk_snapshot_id": payload.get("risk_snapshot_id") or f"rs-{signal['signal_id']}",
                "task_id": task_id,
                "capital_budget": str(risk.get("risk_budget_delta") or "0"),
                "max_risk": str(risk.get("worst_case_loss") or "0"),
                "requires_approval": needs_approval,
                "action": action,
                "direction": "POSITIVE" if signal.get("side") == "BUY" else "NEGATIVE",
                "confidence": signal.get("strength"),
                "impact_horizon": "1D",
                "entry_plan": build_entry_plan(
                    action=action,
                    price=suggested,
                    horizon="15m",
                    max_capital_ratio="0.03",
                ),
                "exit_plan": build_exit_plan(action=action, horizon="1D", event_type="SIGNAL"),
                "evidence_refs": signal.get("evidence_refs") or [],
                "payload": {"can_apply": False, "live_blocked": True, "mode": self.mode},
            }
        )

    def _refresh_shadow_outcomes(self, session: BotSession) -> None:
        try:
            session.use("query_positions")
            positions = self.gateway.get_positions(
                self.market, account_id=self.account_id
            )
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            return
        marks = {
            row.get("symbol"): row.get("avg_cost")
            for row in positions
            if row.get("symbol") and row.get("avg_cost") not in (None, "")
        }
        now = self.now_fn().isoformat()
        for task in session.tasks.find_by_status("SHADOW_RECORDED"):
            payload = dict(task["payload"] or {})
            decision = dict(payload.get("shadow_decision") or {})
            if not decision:
                continue
            mark = marks.get(payload.get("symbol"))
            if mark is None:
                continue
            decision["outcome_price"] = str(mark)
            decision["outcome_at"] = now
            decision["simulated_pnl"] = simulated_pnl(
                str(decision.get("action") or ""),
                decision.get("suggested_price"),
                mark,
                decision.get("quantity"),
            )
            payload["shadow_decision"] = decision
            session.tasks.update_payload(task["task_id"], payload)

    def _preview(self, session: BotSession, task_id: str, signal: dict) -> dict | None:
        session.use("preview_order")
        intent = OrderIntent(
            idempotency_key=f"preview-{signal['signal_id']}",
            market=self.market,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=self._signal_quantity(signal),
            valid_until=self.now_fn() + timedelta(minutes=10),
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id=f"rs-{signal['signal_id']}",
            order_type="MARKET",
        )
        try:
            preview = self.gateway.preview_order(intent)
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
            if self.mode != "shadow":
                session.tasks.transition(task_id, "PRE_SUBMIT_FAILED")
            session.memory.remember(
                f"订单预览失败（{signal['signal_id']}）: {exc}", kind="error",
                tags=["preview-failed", f"signal:{signal['signal_id']}"],
            )
            return None

        risk = preview.get("risk", {})
        baseline_position = "0"
        baseline_cash = "0"
        try:
            session.use("query_positions")
            positions = self.gateway.get_positions(
                self.market, account_id=self.account_id
            )
            session.use("query_accounts")
            accounts = self.gateway.get_account_summary(self.market)
            pos = next(
                (p for p in positions if p.get("symbol") == signal["symbol"]),
                None,
            )
            acct = next(
                (a for a in accounts if a.get("account_id") == self.account_id),
                accounts[0] if accounts else None,
            )
            if pos is not None:
                baseline_position = str(pos.get("quantity", "0"))
            if acct is not None:
                baseline_cash = str(acct.get("cash", "0"))
        except Exception as exc:
            if _gateway_status(exc) is None:
                raise
        task = session.tasks.get(task_id)
        payload = dict(task["payload"])
        payload.update(
            risk_snapshot_id=risk.get("risk_snapshot_id", f"rs-{signal['signal_id']}"),
            position_after=str(risk.get("position_after", "0")),
            risk_budget_delta=str(risk.get("risk_budget_delta", "0")),
            worst_case_loss=str(risk.get("worst_case_loss", "0")),
            quantity=str(intent.quantity),
            valid_until=intent.valid_until.isoformat(),
            idempotency_key=_new_idempotency_key(self.idempotency_prefix),
            reconcile_baseline={
                "position_quantity": baseline_position,
                "cash": baseline_cash,
                "side": signal["side"],
                "symbol": signal["symbol"],
            },
            risk_boundary={
                "max_quantity": str(risk.get("position_after", intent.quantity)),
                "max_notional": str(risk.get("risk_budget_delta", "0")),
                "max_worst_case_loss": str(risk.get("worst_case_loss", "0")),
                "max_slippage": str(preview.get("estimated_slippage", "0")),
                "strategy_version": signal["strategy_version"],
                "account_id": self.account_id,
                "market": self.market.value,
                "signal_id": signal["signal_id"],
            },
        )
        session.tasks.update_payload(
            task_id, payload, idempotency_key=payload["idempotency_key"]
        )
        session.tasks.transition(task_id, "PREVIEWED")
        self._sync_execution_ledger(
            session,
            payload,
            task_id=task_id,
            status="PREVIEWED",
            preview=preview,
        )
        return preview

    def _request_approval(self, session: BotSession, task_id: str,
                          signal: dict) -> str | None:
        task_row = session.tasks.get(task_id)
        payload = dict(task_row["payload"])
        binding = {
            "market": self.market.value,
            "account_id": self.account_id,
            "symbol": payload.get("symbol", signal["symbol"]),
            "side": payload.get("side", signal["side"]),
            "order_type": "MARKET",
            "quantity": payload.get("quantity", signal.get("quantity", "0.01")),
            "limit_price": None,
            "strategy_version": payload.get(
                "strategy_version", signal["strategy_version"]
            ),
            "signal_snapshot_id": payload.get("signal_id", signal["signal_id"]),
            "risk_snapshot_id": payload.get("risk_snapshot_id"),
            "valid_until": payload.get("valid_until"),
        }
        try:
            approval_id = self.approvals.request(
                market=self.market.value,
                requested_by_bot=self.name,
                subject_type="order",
                subject_id=payload.get("signal_id", signal["signal_id"]),
                evidence_refs=[
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
                binding=binding,
            )
        except Exception as exc:
            session.tasks.transition(task_id, "PRE_SUBMIT_FAILED")
            session.memory.remember(
                f"审批请求失败（{signal['signal_id']}）: {exc}", kind="error",
                tags=[f"signal:{signal['signal_id']}"],
            )
            return None
        task_row = session.tasks.get(task_id)
        payload = dict(task_row["payload"])
        payload["approval_requested_at"] = datetime.now(UTC).isoformat()
        session.tasks.update_payload(task_id, payload)
        self._sync_execution_ledger(
            session,
            payload,
            task_id=task_id,
            status="AWAITING_APPROVAL",
            approval_id=approval_id,
            extra_payload={
                "approval_requested_at": payload["approval_requested_at"],
                "binding": binding,
            },
        )
        return approval_id
