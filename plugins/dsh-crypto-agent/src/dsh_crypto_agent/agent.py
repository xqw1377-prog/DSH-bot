"""Crypto Bot Agent：数字资产专业 Bot。

执行闭环（审批/再校验/提交/崩溃恢复/对账）全部来自共享的
TradeExecutionCore——本文件只声明市场、账户与 Bot 身份。
禁止在本插件内复制或改写执行流程（防止与 A 股 Bot 分叉）。
"""

from dsh_contracts import Market
from dsh_gateway_client import GatewayClient
from dsh_trade_approval import ApprovalWorkflow
from dsh_trade_core import MarketPolicy, TradeExecutionCore


class CryptoAgent(TradeExecutionCore):
    name = "crypto-bot"
    market = Market.CRYPTO

    def __init__(
        self,
        gateway: GatewayClient,
        approvals: ApprovalWorkflow,
        account_id: str,
        min_strength: float = 0.6,
    ):
        super().__init__(
            gateway=gateway, approvals=approvals, account_id=account_id,
            min_strength=min_strength, policy=MarketPolicy(),
        )
