"""A 股专业 Bot。

执行闭环（审批/再校验/提交/崩溃恢复/对账）全部复用 TradeExecutionCore；
A 股特有规则（交易时段与午休、周末休市、100 股整手、±10% 涨跌停、T+1）
由 AStockMarketPolicy 注入，不复制任何执行代码。
"""

from dsh_contracts import Market
from dsh_gateway_client import GatewayClient
from dsh_trade_approval import ApprovalWorkflow
from dsh_trade_core import AStockMarketPolicy, TradeExecutionCore


class AStockAgent(TradeExecutionCore):
    name = "a-stock-bot"
    market = Market.A_SHARE

    def __init__(
        self,
        gateway: GatewayClient,
        approvals: ApprovalWorkflow,
        account_id: str,
        min_strength: float = 0.6,
        policy: AStockMarketPolicy | None = None,
    ):
        super().__init__(
            gateway=gateway, approvals=approvals, account_id=account_id,
            min_strength=min_strength, policy=policy or AStockMarketPolicy(),
        )
