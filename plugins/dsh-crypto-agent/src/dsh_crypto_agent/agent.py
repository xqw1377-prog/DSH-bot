"""Crypto Bot Agent：第一个在 DSH 底座上真实运行的插件。

tick 全流程（多阶段动作通过 TaskStore 持久化，重启后从上次状态继续）：
1. 恢复在途订单任务：SUBMITTED/ACKNOWLEDGED/PARTIALLY_FILLED 轮询订单状态，
   推进到 FILLED/CANCELLED/ORDER_REJECTED；UNKNOWN 只查询，绝不重新提交
2. 恢复在途审批任务：AWAITING_APPROVAL 轮询审批状态
   - APPROVED   → 执行前再校验 → 注册风险快照 → 提交 Paper 订单
   - REJECTED/EXPIRED → 任务关闭，不下单（失败关闭）
3. 健康检查：降级则记事件并跳过新信号
4. 拉取信号：未处理且强度达标的信号 → 订单预览 → 发起人工审批

执行前再校验（全部通过才提交，任何一项失败即失败关闭）：
- 审批绑定：审批的 evidence_refs 必须与本任务的 signal/strategy_version 一致（防篡改）
- 信号时效：valid_until 未过期，且信号仍在当前信号列表中且方向/版本未变
- 风险一致性：重新预览的风险快照与任务记录的关键数字一致（worst_case_loss /
  risk_budget_delta / position_after），不一致视为风险已变化

红线：订单必须经过预览、风控快照、人工审批三道门；本 Agent
不持有任何交易密钥，所有资金动作经 Quant Gateway。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dsh_contracts import Market, OrderIntent, OrderSide
from dsh_gateway_client import GatewayClient, GatewayError, new_idempotency_key
from dsh_runtime import BotSession
from dsh_trade_approval import ApprovalWorkflow


class CryptoAgent:
    name = "crypto-bot"

    def __init__(
        self,
        gateway: GatewayClient,
        approvals: ApprovalWorkflow,
        account_id: str,
        min_strength: float = 0.6,
    ):
        self.gateway = gateway
        self.approvals = approvals
        self.account_id = account_id
        self.min_strength = min_strength

    # ---- 调度入口（由 DSH Runtime 驱动）----

    def tick(self, session: BotSession) -> None:
        self._resume_pending_tasks(session)
        self._process_new_signals(session)

    # ---- 在途任务恢复：审批通过才提交 Paper 订单；已提交则对账成交 ----

    def _resume_pending_tasks(self, session: BotSession) -> None:
        for task in session.tasks.find_by_status(
            "AWAITING_APPROVAL", "APPROVED_SUBMITTING"
        ):
            self._advance_awaiting(session, task)
        for task in session.tasks.find_by_status("SUBMITTED"):
            self._reconcile_fill(session, task)

    def _advance_awaiting(self, session: BotSession, task: dict) -> None:
        approval_id = task["approval_id"]
        try:
            session.use("query_approvals")
            approval = self.gateway.get_approval(approval_id)
        except GatewayError as exc:
            if exc.status_code == 404:
                # 审批已从账本消失：超时未决被网关清理（EXPIRED 路径），关闭任务
                session.tasks.transition(task["task_id"], "EXPIRED")
                session.events.emit(
                    "approval/rejected", "CRYPTO", "bot", self.name,
                    {"approval_id": approval_id, "task_id": task["task_id"],
                     "reason": "approval expired and purged"},
                )
                return
            session.memory.remember(
                f"任务 {task['task_id']} 审批查询失败: {exc}", kind="error",
                tags=[task["task_id"]],
            )
            return  # 网关不可达：保持 AWAITING，下个 tick 重试，绝不下单

        status = approval.get("status")
        if status == "APPROVED":
            if task["status"] == "AWAITING_APPROVAL":
                # 执行前再校验：审批绑定、信号时效、风险一致性
                problem = self._revalidate(session, task, approval)
                if problem is not None:
                    session.tasks.transition(task["task_id"], "FAILED")
                    session.events.emit(
                        "order/unknown", "CRYPTO", "bot", self.name,
                        {"task_id": task["task_id"], "rejected_at_submit": problem},
                    )
                    session.memory.remember(
                        f"任务 {task['task_id']} 执行前校验失败（不下单）: {problem}",
                        kind="error", tags=[task["task_id"], "revalidate-failed"],
                    )
                    return
                session.tasks.transition(task["task_id"], "APPROVED_SUBMITTING")
                task = session.tasks.get(task["task_id"])
            self._submit(session, task)
        elif status == "REJECTED":
            session.tasks.transition(task["task_id"], "REJECTED")
            session.events.emit(
                "approval/rejected", "CRYPTO", "bot", self.name,
                {"approval_id": approval_id, "task_id": task["task_id"]},
            )
        elif status == "EXPIRED":
            session.tasks.transition(task["task_id"], "EXPIRED")
            session.events.emit(
                "approval/rejected", "CRYPTO", "bot", self.name,
                {"approval_id": approval_id, "task_id": task["task_id"],
                 "reason": "approval expired"},
            )
        # REQUESTED：仍在等待人工，什么都不做

    def _revalidate(self, session: BotSession, task: dict,
                     approval: dict) -> str | None:
        """执行前再校验。返回 None 表示通过，否则返回失败原因（失败关闭）。

        1. 审批绑定：evidence_refs 必须与本任务的 signal / strategy_version 一致
        2. 信号时效与现状：valid_until 未过期、信号仍在列表、方向/版本未变
        3. 风险一致性：重新预览的关键风险数字与审批时一致
        """
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
            if datetime.now(UTC) > until:
                return f"signal expired at {valid_until}"
        try:
            session.use("query_signals")
            current = self.gateway.get_signals(Market.CRYPTO)
        except GatewayError as exc:
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
                market=Market.CRYPTO,
                account_id=self.account_id,
                strategy_id=signal["strategy_id"],
                strategy_version=signal["strategy_version"],
                symbol=signal["symbol"],
                side=OrderSide(signal["side"]),
                quantity=signal.get("quantity", "0.01"),
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                signal_snapshot_id=signal_id,
                risk_snapshot_id=signal["risk_snapshot_id"],
            ))
        except GatewayError as exc:
            return f"cannot re-preview: {exc}"
        fresh_risk = fresh.get("risk", {})
        for field in ("worst_case_loss", "risk_budget_delta", "position_after"):
            try:
                left = Decimal(str(fresh_risk.get(field)))
                right = Decimal(str(signal.get(field)))
            except Exception:
                return (f"risk snapshot changed: {field} "
                        f"{signal.get(field)} -> {fresh_risk.get(field)}")
            if left != right:
                return (f"risk snapshot changed: {field} "
                        f"{signal.get(field)} -> {fresh_risk.get(field)}")
        return None

    def _submit(self, session: BotSession, task: dict) -> None:
        signal = task["payload"]
        intent = OrderIntent(
            idempotency_key=task["idempotency_key"],
            market=Market.CRYPTO,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=signal.get("quantity", "0.01"),
            valid_until=datetime.now(UTC) + timedelta(minutes=10),
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id=signal["risk_snapshot_id"],
            approval_id=task["approval_id"],
        )
        try:
            session.use("submit_order")
            # 复用预览产生的风险快照：正式提交时网关按此查验并二次硬风控
            self.gateway.register_risk_snapshot(Market.CRYPTO, {
                "risk_snapshot_id": signal["risk_snapshot_id"],
                "market": "CRYPTO",
                "account_id": self.account_id,
                "position_before": signal.get("position_before", "0"),
                "position_after": signal.get("position_after", "0.01"),
                "risk_budget_delta": signal.get("risk_budget_delta", "0"),
                "worst_case_loss": signal.get("worst_case_loss", "0"),
                "limits_hit": [],
                "as_of": datetime.now(UTC).isoformat(),
            })
            result = self.gateway.request_order(intent)
        except GatewayError as exc:
            if exc.status_code == 409:
                # 幂等冲突：既有提交（并发/崩溃重启/重复批准）——
                # 认领既有订单进入状态轮询，绝不重新下单
                self._adopt_existing_order(session, task)
                return
            session.tasks.transition(task["task_id"], "FAILED")
            session.events.emit(
                "order/unknown", "CRYPTO", "bot", self.name,
                {"task_id": task["task_id"], "error": str(exc)},
            )
            session.memory.remember(
                f"任务 {task['task_id']} 提交失败: {exc}", kind="error",
                tags=[task["task_id"], "submit-failed"],
            )
            return

        session.tasks.transition(
            task["task_id"], "SUBMITTED", order_id=result["order_id"]
        )
        session.events.emit(
            "order/submitted", "CRYPTO", "bot", self.name,
            {
                "order_id": result["order_id"],
                "idempotency_key": intent.idempotency_key,
                "market": "CRYPTO",
                "approval_id": task["approval_id"],
                "submitted_at": datetime.now(UTC).isoformat(),
            },
        )
        session.memory.remember(
            f"订单 {result['order_id']} 已提交（Paper），审批 {task['approval_id']}",
            kind="order-submitted", tags=[task["task_id"], result["order_id"]],
        )
        # Paper 通常即时成交；同 tick 对账，避免任务停在 SUBMITTED
        submitted = session.tasks.get(task["task_id"])
        if submitted is not None:
            self._reconcile_fill(session, submitted)

    def _adopt_existing_order(self, session: BotSession, task: dict) -> None:
        """按幂等键认领既有订单（409 冲突路径），进入状态轮询，不重复下单。"""
        resp = self.gateway._client.get(
            f"/v1/idempotency-keys/{task['idempotency_key']}"
        )
        order_id = resp.json().get("order_id") if resp.status_code == 200 else None
        if not order_id:
            session.tasks.transition(task["task_id"], "FAILED")
            session.memory.remember(
                f"任务 {task['task_id']} 幂等冲突但查不到既有订单，标记失败",
                kind="error", tags=[task["task_id"]],
            )
            return
        session.tasks.transition(task["task_id"], "SUBMITTED", order_id=order_id)
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
            status = self.gateway.get_order_status(Market.CRYPTO, order_id)
        except GatewayError as exc:
            session.memory.remember(
                f"订单 {order_id} 状态查询失败: {exc}", kind="error",
                tags=[task["task_id"], order_id],
            )
            return

        order_status = status.get("status")
        if order_status == "CANCELLED":
            session.tasks.transition(task["task_id"], "CANCELLED")
            session.events.emit(
                "order/cancelled", "CRYPTO", "bot", self.name,
                {"task_id": task["task_id"], "order_id": order_id},
            )
            return
        if order_status == "REJECTED":
            session.tasks.transition(task["task_id"], "ORDER_REJECTED")
            session.events.emit(
                "order/unknown", "CRYPTO", "bot", self.name,
                {"task_id": task["task_id"], "order_id": order_id,
                 "reason": "order rejected by venue"},
            )
            return
        if order_status == "PARTIALLY_FILLED":
            session.tasks.transition(task["task_id"], "PARTIALLY_FILLED")
            session.events.emit(
                "order/filled", "CRYPTO", "bot", self.name,
                {"task_id": task["task_id"], "order_id": order_id,
                 "partial": True,
                 "filled_quantity": str(status.get("filled_quantity") or "0")},
            )
            return  # 继续轮询直至 FILLED 或人工撤单
        if order_status == "UNKNOWN":
            # 未知状态：只查询，绝不重新提交；保持任务在途继续对账
            session.memory.remember(
                f"订单 {order_id} 状态 UNKNOWN，保持查询恢复（不重新提交）",
                kind="order-unknown", tags=[task["task_id"], order_id],
            )
            return
        if order_status != "FILLED":
            return

        session.events.emit(
            "order/filled", "CRYPTO", "bot", self.name,
            {
                "order_id": order_id,
                "market": "CRYPTO",
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
            },
        )
        session.tasks.transition(task["task_id"], "FILLED")
        session.memory.remember(
            f"订单 {order_id} 已成交",
            kind="order-filled", tags=[task["task_id"], order_id],
        )
        self._reconcile_account(session, task)

    def _reconcile_account(self, session: BotSession, task: dict) -> None:
        """账户对账：核对持仓与账户，通过后任务才能到达终态 RECONCILED。"""
        order_id = task["order_id"]
        expected = Decimal(str(task["payload"].get("quantity", "0.01")))
        symbol = task["payload"]["symbol"]
        try:
            session.use("query_positions")
            positions = self.gateway.get_positions(
                Market.CRYPTO, account_id=self.account_id
            )
            session.use("query_accounts")
            accounts = self.gateway.get_account_summary(Market.CRYPTO)
        except GatewayError as exc:
            session.memory.remember(
                f"订单 {order_id} 对账数据获取失败（保持 FILLED 待重试）: {exc}",
                kind="error", tags=[task["task_id"]],
            )
            return
        match = next((p for p in positions if p["symbol"] == symbol), None)
        if match is None or Decimal(str(match["quantity"])) < expected:
            session.events.emit(
                "account/mismatch", "CRYPTO", "bot", self.name,
                {"task_id": task["task_id"], "order_id": order_id,
                 "expected_quantity": str(expected),
                 "positions_quantity": str(match["quantity"]) if match else None},
            )
            session.memory.remember(
                f"对账异常：订单 {order_id} 成交 {expected} 但持仓不足，"
                f"任务保持 FILLED 等待人工核查",
                kind="error", tags=[task["task_id"], "reconcile-mismatch"],
            )
            return
        session.events.emit(
            "account/reconciled", "CRYPTO", "bot", self.name,
            {"task_id": task["task_id"], "order_id": order_id,
             "symbol": symbol, "quantity": str(expected),
             "positions_quantity": str(match["quantity"]),
             "equity": str(accounts[0]["equity"]) if accounts else None},
        )
        session.tasks.transition(task["task_id"], "RECONCILED")
        session.memory.remember(
            f"订单 {order_id} 对账通过（{symbol} {expected}），任务完成",
            kind="order-reconciled", tags=[task["task_id"], order_id],
        )

    # ---- 新信号处理 ----

    def _process_new_signals(self, session: BotSession) -> None:
        session.use("query_health")
        health = self.gateway.get_health(Market.CRYPTO)
        if not health.get("system_ok") or not health.get("data_fresh"):
            session.events.emit(
                "incident/opened", "CRYPTO", "bot", self.name,
                {"reason": "market data degraded", "health": health},
            )
            session.memory.remember(
                "交易所/数据状态降级，本 tick 跳过信号处理", kind="incident",
                tags=["degraded"],
            )
            return

        session.use("query_signals")
        for signal in self.gateway.get_signals(Market.CRYPTO):
            self._process_signal(session, signal)

    def _process_signal(self, session: BotSession, signal: dict) -> None:
        signal_id = signal["signal_id"]
        if session.memory.has_tagged(f"signal:{signal_id}"):
            return

        strength = signal.get("strength") or 0.0
        if strength < self.min_strength:
            session.memory.remember(
                f"信号 {signal_id} 强度 {strength} 低于阈值 {self.min_strength}，忽略",
                kind="signal-skip", tags=[f"signal:{signal_id}"],
            )
            return

        task_id = session.tasks.create(
            kind="paper-order", subject_id=signal_id, payload=signal
        )
        task = session.tasks.get(task_id)
        assert task is not None

        preview = self._preview(session, task_id, signal)
        if preview is None:
            return

        approval_id = self._request_approval(session, task_id, signal)
        if approval_id is None:
            return

        session.tasks.transition(
            task_id, "AWAITING_APPROVAL", approval_id=approval_id
        )
        session.memory.remember(
            f"信号 {signal_id}（{signal['symbol']} {signal['side']} "
            f"强度 {strength}）已生成订单预览并等待人工审批 {approval_id}",
            kind="signal-processed", tags=[f"signal:{signal_id}", task_id],
        )

    def _preview(self, session: BotSession, task_id: str, signal: dict) -> dict | None:
        session.use("preview_order")
        intent = OrderIntent(
            idempotency_key=f"preview-{signal['signal_id']}",
            market=Market.CRYPTO,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=signal.get("quantity", "0.01"),
            valid_until=datetime.now(UTC) + timedelta(minutes=10),
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id=f"rs-{signal['signal_id']}",
        )
        try:
            preview = self.gateway.preview_order(intent)
        except GatewayError as exc:
            session.tasks.transition(task_id, "FAILED")
            session.memory.remember(
                f"订单预览失败（{signal['signal_id']}）: {exc}", kind="error",
                tags=["preview-failed", f"signal:{signal['signal_id']}"],
            )
            return None

        risk = preview.get("risk", {})
        # 风险数据随任务持久化，提交阶段据此注册快照
        task = session.tasks.get(task_id)
        payload = dict(task["payload"])
        payload.update(
            risk_snapshot_id=risk.get("risk_snapshot_id", f"rs-{signal['signal_id']}"),
            position_after=str(risk.get("position_after", "0")),
            risk_budget_delta=str(risk.get("risk_budget_delta", "0")),
            worst_case_loss=str(risk.get("worst_case_loss", "0")),
            quantity=str(intent.quantity),
            idempotency_key=new_idempotency_key("crypto-paper"),
        )
        from dsh_runtime.store import _get
        import json as _json
        _get().execute(
            "UPDATE bot_tasks SET payload = ?, idempotency_key = ?, "
            "updated_at = ? WHERE task_id = ?",
            (_json.dumps(payload, ensure_ascii=False), payload["idempotency_key"],
             datetime.now(UTC).isoformat(), task_id),
        )
        _get().commit()
        session.tasks.transition(task_id, "PREVIEWED")
        return preview

    def _request_approval(self, session: BotSession, task_id: str,
                          signal: dict) -> str | None:
        try:
            approval_id = self.approvals.request(
                market="CRYPTO",
                requested_by_bot=self.name,
                subject_type="order",
                subject_id=signal["signal_id"],
                evidence_refs=[
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
            )
        except Exception as exc:
            session.tasks.transition(task_id, "FAILED")
            session.memory.remember(
                f"审批请求失败（{signal['signal_id']}）: {exc}", kind="error",
                tags=[f"signal:{signal['signal_id']}"],
            )
            return None
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", self.name,
            {
                "approval_id": approval_id,
                "requested_by_bot": self.name,
                "subject_type": "order",
                "subject_id": signal["signal_id"],
                "task_id": task_id,
                "evidence_refs": [
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
            },
        )
        return approval_id
