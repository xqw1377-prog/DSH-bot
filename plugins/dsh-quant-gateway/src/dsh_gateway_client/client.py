"""Quant Gateway 客户端。

DSH 侧所有 Bot / 插件访问量化系统的唯一通道（PRD 1.2）：
- 不持有交易密钥，凭据永远留在量化系统侧
- 只读接口直接调用；资金动作（下单）必须携带已审批的 approval_id
- 幂等键由调用方生成并记录，重放会得到 409
"""

from uuid import uuid4

import httpx
from dsh_contracts import Market, OrderIntent


class GatewayError(RuntimeError):
    """网关拒绝或不可达。失败关闭：调用方不得重试资金动作。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"gateway {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class GatewayClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8001", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ---- 只读 ----

    def _get(self, path: str, params: dict | None = None) -> object:
        resp = self._client.get(path, params=params)
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def get_health(self, market: Market):
        return self._get(f"/v1/markets/{market.value}/health")

    def get_positions(self, market: Market, account_id: str | None = None):
        params = {"account_id": account_id} if account_id else None
        return self._get(f"/v1/markets/{market.value}/positions", params)

    def get_account_summary(self, market: Market):
        return self._get(f"/v1/markets/{market.value}/accounts")

    def get_signals(self, market: Market):
        return self._get(f"/v1/markets/{market.value}/signals")

    def preview_order(self, intent: OrderIntent):
        resp = self._client.post(
            f"/v1/markets/{intent.market.value}/orders/preview",
            json=intent.model_dump(mode="json"),
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def get_order_status(self, market: Market, order_id: str):
        return self._get(f"/v1/markets/{market.value}/orders/{order_id}")

    def register_risk_snapshot(self, market: Market, snapshot: dict) -> dict:
        """把预览得到的风险快照注册到网关，供正式提交时查验。"""
        resp = self._client.post(
            f"/v1/markets/{market.value}/risk-snapshots", json=snapshot
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    # ---- 审批 ----

    def request_approval(
        self,
        market: Market,
        requested_by_bot: str,
        subject_type: str,
        subject_id: str,
        evidence_refs: list[str] | None = None,
    ):
        """Bot 只能请求审批，不能替人决定。"""
        resp = self._client.post("/v1/approvals", json={
            "market": market.value,
            "requested_by_bot": requested_by_bot,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "evidence_refs": evidence_refs or [],
        })
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def list_approvals(self, status: str | None = None, market: Market | None = None):
        params = {k: v for k, v in {
            "status": status, "market": market.value if market else None
        }.items() if v}
        return self._get("/v1/approvals", params or None)

    def get_approval(self, approval_id: str) -> dict:
        """查询单个审批状态。Bot 轮询审批结果用，不改变状态。"""
        return self._get(f"/v1/approvals/{approval_id}")

    def decide_approval(self, approval_id: str, decision: str, decided_by: str):
        """仅供人工审批界面使用，Bot 禁止调用。"""
        resp = self._client.post(
            f"/v1/approvals/{approval_id}/decide",
            json={"decision": decision, "decided_by": decided_by},
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    # ---- 资金动作 ----

    def request_order(self, intent: OrderIntent) -> dict:
        """提交订单意图。任何 GatewayError 都不得盲目重试（幂等键除外）。"""
        resp = self._client.post(
            f"/v1/markets/{intent.market.value}/orders",
            json=intent.model_dump(mode="json"),
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def cancel_order(self, market: Market, order_id: str) -> dict:
        resp = self._client.post(
            f"/v1/markets/{market.value}/orders/{order_id}/cancel"
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    # ---- 控制动作（需更强授权）----

    def pause_strategy(self, market: Market, strategy_id: str) -> dict:
        resp = self._client.post(
            f"/v1/markets/{market.value}/strategies/{strategy_id}/pause"
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def resume_strategy(self, market: Market, strategy_id: str) -> dict:
        resp = self._client.post(
            f"/v1/markets/{market.value}/strategies/{strategy_id}/resume"
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()

    def emergency_stop(self, market: Market, account_id: str | None = None) -> dict:
        """Kill Switch：独立控制通道，不依赖 LLM。凭据由 Gateway 管理。"""
        params = {"account_id": account_id} if account_id else None
        resp = self._client.post(
            f"/v1/markets/{market.value}/emergency-stop", params=params
        )
        if resp.is_error:
            raise GatewayError(resp.status_code, resp.text)
        return resp.json()


def new_idempotency_key(prefix: str = "dsh") -> str:
    """生成幂等键。调用方必须先持久化再使用，防止重试产生双单。"""
    return f"{prefix}-{uuid4().hex}"
