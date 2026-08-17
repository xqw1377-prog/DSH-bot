"""Risk Policy 客户端：供 Incident Center 拉取风控规则违反事件。

设计红线：Kill Switch 只接受 risk-policy 签发的结构化 CRITICAL 事件，
LLM/Agent 文本判断只能产生告警，不能自动停盘。
"""

import httpx


class RiskPolicyError(RuntimeError):
    """risk-policy 不可达或返回错误。失败关闭：调用方不得自行降级判断。"""


class RiskPolicyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8003", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def list_critical_violations(
        self, market: str | None = None, acknowledged: bool = False,
    ) -> list[dict]:
        """拉取未确认的 CRITICAL 风控规则违反事件。

        返回的每条事件必须满足 source == "risk-policy" 且 severity == "CRITICAL"，
        Incident Center 才会触发 Kill Switch。
        """
        params = {"severity": "CRITICAL", "acknowledged": str(acknowledged).lower()}
        if market:
            params["market"] = market
        resp = self._client.get("/v1/rule-violations", params=params)
        if resp.is_error:
            raise RiskPolicyError(
                f"risk-policy list violations failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def acknowledge(self, violation_id: str) -> None:
        """Kill Switch 执行后确认该事件，避免重复触发。"""
        resp = self._client.delete(f"/v1/rule-violations/{violation_id}")
        if resp.is_error:
            raise RiskPolicyError(
                f"acknowledge {violation_id} failed: {resp.status_code} {resp.text}"
            )

    def report_violation(self, violation: dict) -> dict:
        """供监控器/测试上报规则违反（生产监控器也可直接调用）。"""
        resp = self._client.post("/v1/rule-violations", json=violation)
        if resp.is_error:
            raise RiskPolicyError(
                f"report violation failed: {resp.status_code} {resp.text}"
            )
        return resp.json()
