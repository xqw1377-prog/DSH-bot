"""Bot 侧交易审批工作流（PRD 11.1 审批门禁）。

设计红线：本模块没有任何「替人决定」的方法。Bot 的职责是
把资金动作连同证据提交给人工审批，然后等待结果：
- APPROVED：把 approval_id 附到订单意图上，交给下单流程
- REJECTED：放弃动作并记录原因
- 超时/网关不可达：放弃动作（失败关闭，绝不猜测放行）

不在此处做长阻塞等待生产环境应改用事件订阅，轮询仅为最小实现。
"""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ApprovalOutcome:
    approval_id: str
    status: str  # APPROVED | REJECTED | TIMEOUT | ERROR
    detail: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == "APPROVED"


class ApprovalWorkflow:
    def __init__(
        self,
        gateway_base_url: str = "http://127.0.0.1:8001",
        api_key: str | None = None,
        timeout: float = 5.0,
    ):
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.Client(
            base_url=gateway_base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        market: str,
        requested_by_bot: str,
        subject_type: str,
        subject_id: str,
        evidence_refs: list[str] | None = None,
    ) -> str:
        """创建审批请求，返回 approval_id。Bot 只能走到这一步。"""
        resp = self._client.post("/v1/approvals", json={
            "market": market,
            "requested_by_bot": requested_by_bot,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "evidence_refs": evidence_refs or [],
        })
        resp.raise_for_status()
        return resp.json()["approval_id"]

    def wait_for_decision(
        self,
        approval_id: str,
        poll_interval: float = 2.0,
        max_wait_seconds: float = 300.0,
    ) -> ApprovalOutcome:
        """轮询审批结果直到决定或超时。超时返回 TIMEOUT，不返回猜测。"""
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            try:
                resp = self._client.get(f"/v1/approvals/{approval_id}")
                if resp.status_code == 404:
                    return ApprovalOutcome(approval_id, "ERROR", "approval not found")
                resp.raise_for_status()
                status = resp.json()["status"]
                if status == "APPROVED":
                    return ApprovalOutcome(approval_id, "APPROVED")
                if status == "REJECTED":
                    return ApprovalOutcome(approval_id, "REJECTED")
                if status == "EXPIRED":
                    return ApprovalOutcome(approval_id, "REJECTED", "approval expired")
            except httpx.HTTPError as exc:
                return ApprovalOutcome(approval_id, "ERROR", str(exc))
            time.sleep(poll_interval)
        return ApprovalOutcome(
            approval_id, "TIMEOUT",
            f"no decision within {max_wait_seconds}s; aborting action",
        )

    def run(
        self,
        market: str,
        requested_by_bot: str,
        subject_type: str,
        subject_id: str,
        evidence_refs: list[str] | None = None,
        poll_interval: float = 2.0,
        max_wait_seconds: float = 300.0,
    ) -> ApprovalOutcome:
        """请求审批并等待决定（组合入口）。"""
        try:
            approval_id = self.request(
                market, requested_by_bot, subject_type, subject_id, evidence_refs
            )
        except httpx.HTTPError as exc:
            return ApprovalOutcome("", "ERROR", f"request failed: {exc}")
        return self.wait_for_decision(
            approval_id, poll_interval=poll_interval, max_wait_seconds=max_wait_seconds
        )
