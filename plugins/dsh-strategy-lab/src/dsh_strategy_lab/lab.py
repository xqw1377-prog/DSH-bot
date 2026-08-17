"""Strategy Lab 策略实验室 Agent（PRD 10.4）。

隔离环境：无生产密钥，不直接部署生产，不直接提高风险预算。
职责：
1. 提出假设（hypothesis）
2. 创建研究实验（调用 strategy-evolution /v1/experiments）
3. 运行回测，对比结果，累积证据
4. 提交策略候选（调用 strategy-evolution /v1/candidates），不直接晋级

红线：本插件只能创建候选与实验，不能晋级到 APPROVED/CANARY/PRODUCTION
（那些需要人工审批 + 证据门禁，由 strategy-evolution 状态机把关）。
"""

import httpx

from dsh_contracts import Market
from dsh_runtime import BotSession


class StrategyLab:
    name = "strategy-lab"

    def __init__(
        self,
        evolution_base_url: str = "http://127.0.0.1:8002",
        timeout: float = 5.0,
    ):
        self._client = httpx.Client(
            base_url=evolution_base_url.rstrip("/"), timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def tick(self, session: BotSession) -> None:
        """轮询：把已完成但未提交候选的实验推进为候选。"""
        session.use("compare_results")
        try:
            resp = self._client.get("/v1/experiments")
            resp.raise_for_status()
            experiments = resp.json()
        except httpx.HTTPError as exc:
            session.memory.remember(
                f"拉取实验失败: {exc}", kind="error", tags=["lab:error"]
            )
            return

        for exp in experiments:
            self._maybe_submit_candidate(session, exp)

    def propose(
        self, session: BotSession, market: Market, strategy_id: str,
        hypothesis: str, data_snapshot_id: str,
    ) -> str | None:
        """提出假设并创建实验，返回 experiment_id。"""
        session.use("create_hypothesis")
        try:
            resp = self._client.post("/v1/experiments", json={
                "market": market.value,
                "strategy_id": strategy_id,
                "hypothesis": hypothesis,
                "data_snapshot_id": data_snapshot_id,
                "created_by_bot": self.name,
            })
            resp.raise_for_status()
            exp_id = resp.json()["experiment_id"]
        except httpx.HTTPError as exc:
            session.memory.remember(
                f"创建实验失败（{strategy_id}）: {exc}", kind="error",
                tags=["lab:error", f"strategy:{strategy_id}"],
            )
            return None

        session.events.emit(
            "strategy/hypothesis.created", market.value, "bot", self.name,
            {"experiment_id": exp_id, "strategy_id": strategy_id,
             "hypothesis": hypothesis},
        )
        session.memory.remember(
            f"提出假设: {hypothesis}（实验 {exp_id}）",
            kind="hypothesis", tags=[f"experiment:{exp_id}"],
        )
        return exp_id

    def record_result(
        self, session: BotSession, experiment_id: str, result_ref: str,
    ) -> None:
        """记录实验结果（回测/验证产物引用），作为晋级证据。"""
        session.use("run_experiment")
        session.memory.remember(
            f"实验 {experiment_id} 结果: {result_ref}",
            kind="experiment-result",
            tags=[f"experiment:{experiment_id}", f"evidence:{result_ref}"],
        )

    def submit_candidate(
        self, session: BotSession, experiment_id: str,
        market: Market, strategy_id: str, strategy_version: str,
        evidence_refs: list[str],
    ) -> str | None:
        """提交策略候选。不直接晋级——晋级由 strategy-evolution 状态机 + 审批把关。"""
        session.use("submit_candidate")
        try:
            resp = self._client.post("/v1/candidates", json={
                "market": market.value,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
            })
            resp.raise_for_status()
            candidate_id = resp.json()["candidate_id"]
        except httpx.HTTPError as exc:
            session.memory.remember(
                f"提交候选失败（{strategy_id}）: {exc}", kind="error",
                tags=["lab:error", f"strategy:{strategy_id}"],
            )
            return None

        session.events.emit(
            "candidate/nominated", market.value, "bot", self.name,
            {"candidate_id": candidate_id, "experiment_id": experiment_id,
             "evidence_refs": evidence_refs},
        )
        session.memory.remember(
            f"提交候选 {candidate_id}（实验 {experiment_id}，证据 {len(evidence_refs)} 条）",
            kind="candidate-submitted",
            tags=[f"candidate:{candidate_id}", f"experiment:{experiment_id}"],
        )
        return candidate_id

    def _maybe_submit_candidate(self, session: BotSession, exp: dict) -> None:
        """实验有结果但尚未提交候选时，提示人工决定是否提交。"""
        if exp.get("result_ref") is None:
            return
        exp_id = exp["experiment_id"]
        if session.memory.has_tagged(f"candidate:from:{exp_id}"):
            return
        session.memory.remember(
            f"实验 {exp_id}（策略 {exp['strategy_id']}）已有结果 "
            f"{exp['result_ref']}，建议人工评估是否提交候选",
            kind="advice", tags=[f"candidate:from:{exp_id}"],
        )
