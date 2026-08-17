"""独立风控审计插件（PRD 设计红线：策略晋级不能只依据单次回测）。

职责：对 risk-policy 的决定做独立二次校验，避免单点风控被绕过。
- 订单风控审计：重算最坏损失占比，任何与 risk-policy 结论不一致即记 incident
- 策略晋级审计：检查晋级证据是否充分（>=3 条且非同源），不足即记 incident

红线：本插件不直接拦截订单，只产出审计结论与 incident 事件；
订单拦截由 Quant Gateway 的二次硬风控 + 审批门禁负责。本插件的价值是
独立于 risk-policy 的第二双眼睛。HTTP 服务见 service.py，供 evolution 远程调用。
"""

from dataclasses import dataclass

from dsh_contracts import Market, RiskSnapshot, StrategyCandidate
from dsh_runtime import BotSession


@dataclass(frozen=True)
class AuditVerdict:
    """审计结论。approved=True 表示与 risk-policy 一致，False 表示存疑需人工。"""
    approved: bool
    reason: str

    @property
    def disputed(self) -> bool:
        return not self.approved


class RiskAuditor:
    name = "risk-auditor"

    def __init__(
        self,
        max_loss_ratio_per_order_a_share: float = 0.01,
        max_loss_ratio_per_order_crypto: float = 0.02,
        min_promotion_evidence: int = 3,
    ):
        self.ratio_a = max_loss_ratio_per_order_a_share
        self.ratio_crypto = max_loss_ratio_per_order_crypto
        self.min_promotion_evidence = min_promotion_evidence

    def evaluate_order(
        self,
        market: Market,
        risk: RiskSnapshot,
        equity: str,
        upstream_passed: bool,
    ) -> AuditVerdict:
        """无 Session 的纯审计逻辑（HTTP / 库均可调用）。"""
        try:
            equity_val = float(equity)
        except (TypeError, ValueError):
            return AuditVerdict(False, "equity 不可用")

        if equity_val <= 0:
            return AuditVerdict(False, "equity 非正")

        ratio = float(risk.worst_case_loss) / equity_val
        limit = self.ratio_a if market == Market.A_SHARE else self.ratio_crypto
        my_pass = ratio <= limit

        if my_pass != upstream_passed:
            reason = (
                f"风控结论不一致: risk-policy={upstream_passed}, "
                f"auditor={my_pass} (ratio={ratio:.4f}, limit={limit})"
            )
            return AuditVerdict(False, reason)

        if not my_pass:
            return AuditVerdict(
                False, f"最坏损失占比 {ratio:.4f} 超限 {limit}"
            )

        return AuditVerdict(True, f"ratio={ratio:.4f} 在限内")

    def evaluate_promotion(
        self,
        candidate: StrategyCandidate,
        evidence_refs: list[str],
        upstream_passed: bool = True,
    ) -> AuditVerdict:
        """无 Session 的晋级审计逻辑。"""
        if len(evidence_refs) < self.min_promotion_evidence:
            return AuditVerdict(
                False,
                f"晋级证据不足: {len(evidence_refs)} < {self.min_promotion_evidence}",
            )

        prefixes = {
            ref.split(":", 1)[0] if ":" in ref else ref for ref in evidence_refs
        }
        if len(prefixes) == 1:
            return AuditVerdict(
                False,
                f"晋级证据同源（全部为 {prefixes}），"
                "策略晋级不能只依据单次回测/单一来源",
            )

        if not upstream_passed:
            return AuditVerdict(False, "upstream 未通过，审计亦不通过")

        return AuditVerdict(
            True,
            f"证据 {len(evidence_refs)} 条，来源 {len(prefixes)} 种，充分",
        )

    def audit_order(
        self,
        session: BotSession,
        market: Market,
        risk: RiskSnapshot,
        equity: str,
        upstream_passed: bool,
    ) -> AuditVerdict:
        """对 risk-policy 的订单风控结论做独立二次校验。"""
        session.use("audit_order")
        verdict = self.evaluate_order(market, risk, equity, upstream_passed)
        if verdict.disputed:
            self._emit(session, market, verdict.reason, snapshot=risk)
        return verdict

    def audit_promotion(
        self,
        session: BotSession,
        candidate: StrategyCandidate,
        evidence_refs: list[str],
        upstream_passed: bool,
    ) -> AuditVerdict:
        """对策略晋级做独立审计：证据充分性与同源性。"""
        session.use("audit_promotion")
        verdict = self.evaluate_promotion(candidate, evidence_refs, upstream_passed)
        if verdict.disputed:
            self._emit(session, candidate.market, verdict.reason, candidate=candidate)
        return verdict

    def _emit(
        self, session: BotSession, market: Market, reason: str,
        snapshot: RiskSnapshot | None = None,
        candidate: StrategyCandidate | None = None,
    ) -> None:
        payload: dict = {"reason": reason}
        if snapshot is not None:
            payload["risk_snapshot_id"] = snapshot.risk_snapshot_id
        if candidate is not None:
            payload["candidate_id"] = candidate.candidate_id
        session.events.emit(
            "incident/opened", market.value, "bot", self.name, payload
        )
        session.memory.remember(
            f"风控审计存疑（{market.value}）: {reason}",
            kind="audit-dispute", tags=["risk-auditor", f"market:{market.value}"],
        )
