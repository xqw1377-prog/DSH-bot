"""Crypto Bot Agent：第一个在 DSH 底座上真实运行的插件。

每个 tick 的职责（全部只读 + 发起审批，不直接下单）：
1. 检查市场健康度，降级则记事件并跳过本 tick（失败关闭）
2. 拉取量化系统信号
3. 对未处理过的信号：构造订单意图 → preview_order（不改变资金状态）
4. 通过 dsh-trade-approval 发起人工审批请求（Bot 只请求，不决定）
5. 记忆去重（signal_id），事件留痕

红线：本 Agent 没有 request_order/cancel_order 的调用路径，
下单只能由量化系统在审批通过后自行执行。
"""

from dsh_gateway_client import GatewayClient, GatewayError
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

    def tick(self, session: BotSession) -> None:
        session.use("query_health")
        health = self.gateway.get_health(self._market)
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
        signals = self.gateway.get_signals(self._market)
        for signal in signals:
            self._process_signal(session, signal)

    def _process_signal(self, session: BotSession, signal: dict) -> None:
        signal_id = signal["signal_id"]
        # 记忆去重：处理过的信号不再重复发起审批
        if session.memory.has_tagged(f"signal:{signal_id}"):
            return

        strength = signal.get("strength") or 0.0
        if strength < self.min_strength:
            session.memory.remember(
                f"信号 {signal_id} 强度 {strength} 低于阈值 {self.min_strength}，忽略",
                kind="signal-skip", tags=[f"signal:{signal_id}"],
            )
            return

        preview = self._preview(session, signal)
        if preview is None:
            return

        approval_id = self._request_approval(session, signal)
        if approval_id is None:
            return

        session.memory.remember(
            f"信号 {signal_id}（{signal['symbol']} {signal['side']} "
            f"强度 {strength}）已生成订单预览并等待人工审批 {approval_id}",
            kind="signal-processed", tags=[f"signal:{signal_id}"],
        )

    def _preview(self, session: BotSession, signal: dict) -> dict | None:
        session.use("preview_order")
        from dsh_contracts import Market, OrderIntent, OrderSide
        from datetime import UTC, datetime, timedelta

        intent = OrderIntent(
            idempotency_key=f"preview-{signal['signal_id']}",
            market=Market(signal["market"]),
            account_id=self.account_id,
            strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"],
            symbol=signal["symbol"],
            side=OrderSide(signal["side"]),
            quantity="0.01",
            valid_until=datetime.now(UTC) + timedelta(minutes=10),
            signal_snapshot_id=signal["signal_id"],
            risk_snapshot_id="",  # 预览阶段尚无风险快照
        )
        try:
            return self.gateway.preview_order(intent)
        except GatewayError as exc:
            session.memory.remember(
                f"订单预览失败（{signal['signal_id']}）: {exc}", kind="error",
                tags=["preview-failed", f"signal:{signal['signal_id']}"],
            )
            return None

    def _request_approval(self, session: BotSession, signal: dict) -> str | None:
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
                "evidence_refs": [
                    f"signal:{signal['signal_id']}",
                    f"strategy:{signal['strategy_id']}@{signal['strategy_version']}",
                ],
            },
        )
        return approval_id

    @property
    def _market(self):
        from dsh_contracts import Market
        return Market.CRYPTO
