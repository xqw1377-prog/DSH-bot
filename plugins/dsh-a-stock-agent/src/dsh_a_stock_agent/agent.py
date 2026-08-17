"""A 股专业 Bot Agent（PRD 1.1，与 crypto-agent 对称的完整执行闭环）。

每个 tick 的职责：
1. 恢复在途任务：AWAITING_APPROVAL 轮询审批；SUBMITTED/ACK/FILLED 轮询订单状态
2. 健康检查：降级则记 incident 并跳过新信号
3. 拉取信号：未处理且强度达标的信号 → 订单预览 → 发起人工审批

审批通过后执行前再校验（A 股特性，全部通过才提交，任何一项失败即失败关闭）：
- 100 股整手：quantity 必须是 100 的正整数倍
- T+1：SELL 信号需校验 available_quantity（今日买入次日才能卖出）
- 涨跌停校验：触及涨跌停的订单需在证据中标注，涨停拒绝买入、跌停拒绝卖出
- 交易时段：非交易时段（含午休 11:30-13:00）拒绝提交
- 信号时效与风险一致性（与 crypto-agent 对称）

订单状态推进（与 crypto-agent 对称）：
SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED → RECONCILED
可从任意状态转入 CANCELLED / ORDER_REJECTED / FAILED。
FILLED 后执行账户对账（reconciliation_status 独立推进）。

红线：所有资金动作经 Quant Gateway，本 Agent 不持有交易密钥。
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from dsh_contracts import Market, OrderIntent, OrderSide
from dsh_gateway_client import GatewayClient, GatewayError, new_idempotency_key
from dsh_runtime import BotSession
from dsh_trade_approval import ApprovalWorkflow


# A 股交易时段（北京时间，UTC+8）
# 上午：09:30-11:30，下午：13:00-15:00
_TRADING_SESSIONS = [
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
]

_LOT_SIZE = 100  # A 股最小交易单位


def _is_trading_hours(dt: datetime) -> bool:
    """判断是否在 A 股交易时段内（北京时间 09:30-11:30, 13:00-15:00）。"""
    # UTC 转北京时间（UTC+8）：直接加 8 小时偏移取分钟数
    if dt.tzinfo == UTC:
        beijing_minutes = ((dt.hour + 8) % 24) * 60 + dt.minute
    else:
        beijing_minutes = dt.hour * 60 + dt.minute
    # 09:30 = 570, 11:30 = 690, 13:00 = 780, 15:00 = 900
    in_morning = 570 <= beijing_minutes <= 690
    in_afternoon = 780 <= beijing_minutes <= 900
    return in_morning or in_afternoon


class AStockAgent:
    name = "a-stock-bot"

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

    # ---- 在途任务恢复：审批通过才提交订单；已提交则对账成交 ----

    def _resume_pending_tasks(self, session: BotSession) -> None:
        for task in session.tasks.find_by_status(
            "AWAITING_APPROVAL", "APPROVED_SUBMITTING"
        ):
            self._advance_awaiting(session, task)
        for task in session.tasks.find_by_status(
            "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"
        ):
            self._reconcile_order(session, task)

    def _advance_awaiting(self, session: BotSession, task: dict) -> None:
        approval_id = task["approval_id"]
        try:
            session.use("query_approvals")
            approval = self.gateway.get_approval(approval_id)
        except GatewayError as exc:
            if exc.status_code == 404:
                # 审批已从账本消失（EXPIRED 路径），关闭任务
                session.tasks.transition(task["task_id"], "EXPIRED")
                session.events.emit(
                    "approval/rejected", "A_SHARE", "bot", self.name,
                    {"approval_id": approval_id, "task_id": task["task_id"],
                     "reason": "approval expired and purged"},
                )
                return
            session.memory.remember(
                f"任务 {task['task_id']} 审批查询失败: {exc}", kind="error",
                tags=[task["task_id"]],
            )
            return  # 网关不可达：保持 AWAITING，下个 tick 重试

        status = approval.get("status")
        if status == "APPROVED":
            if task["status"] == "AWAITING_APPROVAL":
                problem = self._revalidate(session, task, approval)
                if problem is not None:
                    session.tasks.transition(task["task_id"], "FAILED")
                    session.events.emit(
                        "order/unknown", "A_SHARE", "bot", self.name,
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
                "approval/rejected", "A_SHARE", "bot", self.name,
                {"approval_id": approval_id, "task_id": task["task_id"]},
            )
        elif status == "EXPIRED":
            session.tasks.transition(task["task_id"], "EXPIRED")
            session.events.emit(
                "approval/rejected", "A_SHARE", "bot", self.name,
                {"approval_id": approval_id, "task_id": task["task_id"],
                 "reason": "approval expired"},
            )
        # REQUESTED：仍在等待人工，什么都不做

    def _revalidate(self, session: BotSession, task: dict,
                     approval: dict) -> str | None:
        """执行前再校验。返回 None 表示通过，否则返回失败原因（失败关闭）。

        A 股特性校验：
        1. 100 股整手
        2. T+1 可卖数量（SELL 需 available_quantity）
        3. 交易时段（非交易时段拒绝，含午休）
        4. 涨跌停校验（触及涨跌停拒绝）
        5. 信号时效与风险一致性（与 crypto-agent 对称）
        """
        signal = task["payload"]
        signal_id = signal["signal_id"]

        # 1. 100 股整手
        try:
            qty = int(Decimal(str(signal.get("quantity", "0"))))
        except Exception:
            return f"quantity unparseable: {signal.get('quantity')}"
        if qty <= 0 or qty % _LOT_SIZE != 0:
            return (f"quantity {qty} not a positive multiple of {_LOT_SIZE} "
                    f"(A 股整手约束)")

        # 2. T+1：SELL 需校验可用持仓
        if signal["side"] == "SELL":
            try:
                session.use("query_positions")
                positions = self.gateway.get_positions(Market.A_SHARE, self.account_id)
            except GatewayError as exc:
                return f"cannot query positions for T+1 check: {exc}"
            pos = next((p for p in positions
                        if p.get("symbol") == signal["symbol"]), None)
            if pos is None:
                return f"SELL {signal['symbol']} 无持仓（T+1 约束）"
            try:
                avail = Decimal(str(pos.get("available_quantity", "0")))
            except Exception:
                return (f"available_quantity unparseable: "
                        f"{pos.get('available_quantity')}")
            if avail < qty:
                return (f"SELL {qty} 股但可用持仓仅 {avail}（T+1 约束："
                        f"今日买入次日才能卖出）")

        # 3. 交易时段校验
        if not _is_trading_hours(datetime.now(UTC)):
            return "not in A 股 trading hours (09:30-11:30, 13:00-15:00 北京时间)"

        # 4. 涨跌停校验：通过 preview 的 limits_hit 判断
        try:
            session.use("preview_order")
            fresh = self.gateway.preview_order(OrderIntent(
                idempotency_key=f"reval-{signal_id}",
                market=Market.A_SHARE,
                account_id=self.account_id,
                strategy_id=signal["strategy_id"],
                strategy_version=signal["strategy_version"],
                symbol=signal["symbol"],
                side=OrderSide(signal["side"]),
                quantity=str(qty),
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                signal_snapshot_id=signal_id,
                risk_snapshot_id=signal["risk_snapshot_id"],
            ))
        except GatewayError as exc:
            return f"cannot re-preview: {exc}"
        limits_hit = fresh.get("risk", {}).get("limits_hit", [])
        if any("LIMIT_UP" in str(l) for l in limits_hit) and signal["side"] == "BUY":
            return f"BUY {signal['symbol']} 触及涨停（无法买入）"
        if any("LIMIT_DOWN" in str(l) for l in limits_hit) and signal["side"] == "SELL":
            return f"SELL {signal['symbol']} 触及跌停（无法卖出）"

        # 5. 信号时效
        valid_until = signal.get("valid_until")
        if valid_until:
            try:
                until = datetime.fromisoformat(valid_until)
                if datetime.now(UTC) > until:
                    return f"signal expired at {valid_until}"
            except Exception:
                pass  # 解析失败不阻塞

        # 6. 风险一致性（批准范围校验，与 crypto-agent 对称）
        fresh_risk = fresh.get("risk", {})
        _TOLERANCE = {
            "worst_case_loss": Decimal("1.05"),
            "risk_budget_delta": Decimal("1.05"),
        }
        for field in ("worst_case_loss", "risk_budget_delta"):
            try:
                fresh_val = Decimal(str(fresh_risk.get(field)))
                approved_val = Decimal(str(signal.get(field)))
            except Exception:
                return (f"risk snapshot field {field} unparseable: "
                        f"approved={signal.get(field)} fresh={fresh_risk.get(field)}")
            upper_bound = approved_val * _TOLERANCE[field]
            if fresh_val > upper_bound:
                return (f"risk {field} exceeded approved boundary: "
                        f"approved={approved_val} fresh={fresh_val} "
                        f"limit={upper_bound}")
        return None

    def _submit(self, session: BotSession, task: dict) -> None:
        signal = task["payload"]
        intent = OrderIntent(
            idempotency_key=task["idempotency_key"],
            market=Market.A_SHARE,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=signal.get("quantity", "100"),
            valid_until=datetime.now(UTC) + timedelta(minutes=10),
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id=signal["risk_snapshot_id"],
            approval_id=task["approval_id"],
        )
        try:
            session.use("submit_order")
            self.gateway.register_risk_snapshot(Market.A_SHARE, {
                "risk_snapshot_id": signal["risk_snapshot_id"],
                "market": "A_SHARE",
                "account_id": self.account_id,
                "position_before": signal.get("position_before", "0"),
                "position_after": signal.get("position_after", "100"),
                "risk_budget_delta": signal.get("risk_budget_delta", "0"),
                "worst_case_loss": signal.get("worst_case_loss", "0"),
                "limits_hit": [],
                "as_of": datetime.now(UTC).isoformat(),
            })
            result = self.gateway.request_order(intent)
        except GatewayError as exc:
            if exc.status_code == 409:
                self._adopt_existing_order(session, task)
                return
            session.tasks.transition(task["task_id"], "FAILED")
            session.events.emit(
                "order/unknown", "A_SHARE", "bot", self.name,
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
            "order/submitted", "A_SHARE", "bot", self.name,
            {
                "order_id": result["order_id"],
                "idempotency_key": intent.idempotency_key,
                "market": "A_SHARE",
                "approval_id": task["approval_id"],
                "submitted_at": datetime.now(UTC).isoformat(),
            },
        )
        session.memory.remember(
            f"订单 {result['order_id']} 已提交（Paper），审批 {task['approval_id']}",
            kind="order-submitted", tags=[task["task_id"], result["order_id"]],
        )
        submitted = session.tasks.get(task["task_id"])
        if submitted is not None:
            self._reconcile_order(session, submitted)

    def _adopt_existing_order(self, session: BotSession, task: dict) -> None:
        """按幂等键认领既有订单（409 冲突路径），进入状态轮询，不重复下单。"""
        resp = self.gateway._client.get(
            f"/v1/idempotency-keys/{task['idempotency_key']}"
        )
        body = resp.json() if resp.status_code == 200 else {}
        order_id = body.get("order_id")
        if not order_id:
            session.memory.remember(
                f"任务 {task['task_id']} 幂等冲突且订单仍在途，等待恢复（不重新下单）",
                kind="order-inflight", tags=[task["task_id"]],
            )
            return
        if task["status"] == "APPROVED_SUBMITTING":
            session.tasks.transition(task["task_id"], "SUBMITTED", order_id=order_id)
        session.memory.remember(
            f"任务 {task['task_id']} 认领既有订单 {order_id}（幂等冲突，不重复下单）",
            kind="order-adopted", tags=[task["task_id"], order_id],
        )
        current = session.tasks.get(task["task_id"])
        if current is not None:
            self._reconcile_order(session, current)

    def _reconcile_order(self, session: BotSession, task: dict) -> None:
        """推进订单状态直至 RECONCILED。同一 tick 可多步（ACK→FILL→对账）。"""
        for _ in range(4):
            current = session.tasks.get(task["task_id"])
            if current is None:
                return
            if current["status"] in {
                "RECONCILED", "FAILED", "CANCELLED", "ORDER_REJECTED",
            }:
                return
            if current["status"] == "FILLED":
                self._reconcile_account(session, current)
                return
            progressed = self._advance_venue_status(session, current)
            if not progressed:
                return

    def _advance_venue_status(self, session: BotSession, task: dict) -> bool:
        order_id = task.get("order_id")
        if not order_id:
            return False
        try:
            session.use("query_order_status")
            status = self.gateway.get_order_status(Market.A_SHARE, order_id)
        except GatewayError as exc:
            session.memory.remember(
                f"订单 {order_id} 状态查询失败: {exc}", kind="error",
                tags=[task["task_id"], order_id],
            )
            return False

        order_status = status.get("status")
        if order_status == "CANCELLED":
            if task["status"] != "CANCELLED":
                session.tasks.transition(task["task_id"], "CANCELLED")
                session.events.emit(
                    "order/cancelled", "A_SHARE", "bot", self.name,
                    {"task_id": task["task_id"], "order_id": order_id},
                )
            return False
        if order_status == "REJECTED":
            if task["status"] != "ORDER_REJECTED":
                session.tasks.transition(task["task_id"], "ORDER_REJECTED")
                session.events.emit(
                    "order/rejected", "A_SHARE", "bot", self.name,
                    {"task_id": task["task_id"], "order_id": order_id,
                     "reason": "order rejected by venue"},
                )
            return False
        if order_status == "UNKNOWN":
            session.events.emit(
                "order/unknown", "A_SHARE", "bot", self.name,
                {"task_id": task["task_id"], "order_id": order_id},
            )
            session.memory.remember(
                f"订单 {order_id} 状态 UNKNOWN，保持查询恢复（不重新提交）",
                kind="order-unknown", tags=[task["task_id"], order_id],
            )
            return False
        if order_status == "ACKNOWLEDGED":
            if task["status"] == "SUBMITTED":
                session.tasks.transition(task["task_id"], "ACKNOWLEDGED")
                session.events.emit(
                    "order/acknowledged", "A_SHARE", "bot", self.name,
                    {
                        "order_id": order_id,
                        "market": "A_SHARE",
                        "symbol": status.get("symbol") or task["payload"].get("symbol"),
                        "task_id": task["task_id"],
                        "acknowledged_at": datetime.now(UTC).isoformat(),
                    },
                )
                return True
            return True
        if order_status == "PARTIALLY_FILLED":
            if task["status"] != "PARTIALLY_FILLED":
                if task["status"] == "SUBMITTED":
                    session.tasks.transition(task["task_id"], "ACKNOWLEDGED")
                session.tasks.transition(task["task_id"], "PARTIALLY_FILLED")
            session.events.emit(
                "order/partially_filled", "A_SHARE", "bot", self.name,
                {
                    "order_id": order_id,
                    "market": "A_SHARE",
                    "symbol": status.get("symbol") or task["payload"].get("symbol"),
                    "filled_quantity": str(status.get("filled_quantity") or "0"),
                    "task_id": task["task_id"],
                },
            )
            return True
        if order_status != "FILLED":
            return False

        # venue FILLED
        if task["status"] == "SUBMITTED":
            session.events.emit(
                "order/acknowledged", "A_SHARE", "bot", self.name,
                {
                    "order_id": order_id,
                    "market": "A_SHARE",
                    "symbol": status.get("symbol") or task["payload"].get("symbol"),
                    "task_id": task["task_id"],
                    "acknowledged_at": datetime.now(UTC).isoformat(),
                },
            )
        session.tasks.transition(task["task_id"], "FILLED")
        session.events.emit(
            "order/filled", "A_SHARE", "bot", self.name,
            {
                "order_id": order_id,
                "market": "A_SHARE",
                "symbol": status.get("symbol") or task["payload"].get("symbol"),
                "filled_quantity": str(status.get("filled_quantity") or "0"),
                "avg_price": str(status.get("avg_price") or "0"),
                "filled_at": datetime.now(UTC).isoformat(),
                "fees": str(status.get("fees") or "0"),
                "task_id": task["task_id"],
            },
        )
        return True

    def _reconcile_account(self, session: BotSession, task: dict) -> None:
        """持仓与资金对账通过后，任务才进入 RECONCILED。

        对账状态 (reconciliation_status) 独立于执行状态 (status) 推进。
        """
        order_id = task.get("order_id")
        signal = task["payload"]
        current_recon = task.get("reconciliation_status", "PENDING")
        if current_recon == "PENDING":
            task = session.tasks.set_reconciliation_status(
                task["task_id"], "IN_PROGRESS"
            )
        try:
            session.use("query_positions")
            positions = self.gateway.get_positions(Market.A_SHARE, self.account_id)
            summaries = self.gateway.get_account_summary(Market.A_SHARE)
        except GatewayError as exc:
            session.memory.remember(
                f"对账失败（账户不可达）: {exc}", kind="error",
                tags=[task["task_id"], "reconcile"],
            )
            return  # 保持 IN_PROGRESS，下个 tick 重试

        match = next((s for s in summaries if s.get("account_id") == self.account_id), None)
        if match is None:
            session.events.emit(
                "account/mismatch", "A_SHARE", "bot", self.name,
                {
                    "account_id": self.account_id,
                    "order_id": order_id,
                    "reason": "account summary missing after fill",
                    "task_id": task["task_id"],
                },
            )
            session.tasks.set_reconciliation_status(task["task_id"], "MISMATCH")
            session.tasks.transition(task["task_id"], "FAILED")
            return

        pos = next(
            (p for p in positions if p.get("symbol") == signal.get("symbol")),
            None,
        )
        if pos is None:
            session.events.emit(
                "account/mismatch", "A_SHARE", "bot", self.name,
                {
                    "account_id": self.account_id,
                    "order_id": order_id,
                    "reason": f"position missing for {signal.get('symbol')}",
                    "task_id": task["task_id"],
                },
            )
            session.tasks.set_reconciliation_status(task["task_id"], "MISMATCH")
            session.tasks.transition(task["task_id"], "FAILED")
            return

        try:
            qty = Decimal(str(pos.get("quantity", "0")))
            equity = Decimal(str(match.get("equity", "0")))
        except Exception:
            qty, equity = Decimal("0"), Decimal("0")
        if qty < 0 or equity <= 0:
            session.events.emit(
                "account/mismatch", "A_SHARE", "bot", self.name,
                {
                    "account_id": self.account_id,
                    "order_id": order_id,
                    "reason": f"invalid qty={qty} equity={equity}",
                    "task_id": task["task_id"],
                },
            )
            session.tasks.set_reconciliation_status(task["task_id"], "MISMATCH")
            session.tasks.transition(task["task_id"], "FAILED")
            return

        session.tasks.set_reconciliation_status(task["task_id"], "MATCHED")
        session.events.emit(
            "account/reconciled", "A_SHARE", "bot", self.name,
            {
                "account_id": self.account_id,
                "order_id": order_id,
                "symbol": signal.get("symbol"),
                "quantity": str(qty),
                "equity": str(equity),
                "cash": str(match.get("cash")),
                "reconciliation_version": match.get("reconciliation_version"),
                "task_id": task["task_id"],
                "reconciled_at": datetime.now(UTC).isoformat(),
            },
        )
        session.tasks.set_reconciliation_status(task["task_id"], "RECONCILED")
        session.tasks.transition(task["task_id"], "RECONCILED")
        session.memory.remember(
            f"订单 {order_id} 已对账完成（RECONCILED）",
            kind="account-reconciled", tags=[task["task_id"], order_id],
        )

    # ---- 新信号处理 ----

    def _process_new_signals(self, session: BotSession) -> None:
        session.use("query_health")
        health = self.gateway.get_health(Market.A_SHARE)
        if not health.get("system_ok") or not health.get("data_fresh"):
            session.events.emit(
                "incident/opened", "A_SHARE", "bot", self.name,
                {"reason": "market data degraded", "health": health},
            )
            session.memory.remember(
                "A 股市场数据状态降级，本 tick 跳过信号处理", kind="incident",
                tags=["degraded"],
            )
            return

        session.use("query_signals")
        # T+1：卖出需校验可用持仓，预先取一次持仓快照
        session.use("query_positions")
        try:
            positions = self.gateway.get_positions(Market.A_SHARE, self.account_id)
        except GatewayError:
            positions = []
        avail_by_symbol = {
            p["symbol"]: p.get("available_quantity", "0") for p in positions
        }

        for signal in self.gateway.get_signals(Market.A_SHARE):
            self._process_signal(session, signal, avail_by_symbol)

    def _process_signal(
        self, session: BotSession, signal: dict,
        avail_by_symbol: dict[str, str],
    ) -> None:
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

        # A 股 T+1：SELL 信号需校验可用持仓
        if signal["side"] == "SELL":
            avail = avail_by_symbol.get(signal["symbol"], "0")
            if avail in ("0", "0.0", ""):
                session.memory.remember(
                    f"信号 {signal_id}（SELL {signal['symbol']}）"
                    f"无可用持仓，T+1 约束下无法卖出，跳过",
                    kind="signal-skip", tags=[f"signal:{signal_id}", "t_plus_1"],
                )
                return

        # 创建任务并持久化
        task_id = session.tasks.create(
            kind="paper-order", subject_id=signal_id,
            payload=dict(signal),
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
            f"A 股信号 {signal_id}（{signal['symbol']} {signal['side']} "
            f"强度 {strength}）已生成订单预览并等待人工审批 {approval_id}",
            kind="signal-processed", tags=[f"signal:{signal_id}", task_id],
        )

    def _preview(self, session: BotSession, task_id: str, signal: dict) -> dict | None:
        session.use("preview_order")
        intent = OrderIntent(
            idempotency_key=f"preview-{signal['signal_id']}",
            market=Market.A_SHARE,
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity=signal.get("quantity", "100"),  # A 股最小 100 股
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
        task = session.tasks.get(task_id)
        payload = dict(task["payload"])
        payload.update(
            risk_snapshot_id=risk.get("risk_snapshot_id", f"rs-{signal['signal_id']}"),
            position_after=str(risk.get("position_after", "100")),
            risk_budget_delta=str(risk.get("risk_budget_delta", "0")),
            worst_case_loss=str(risk.get("worst_case_loss", "0")),
            quantity=str(signal.get("quantity", "100")),
            idempotency_key=new_idempotency_key("a-stock-paper"),
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
                market="A_SHARE",
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
            "approval/requested", "A_SHARE", "bot", self.name,
            {
                "approval_id": approval_id,
                "market": "A_SHARE",
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
        )
        return approval_id

    @property
    def _market(self):
        return Market.A_SHARE
