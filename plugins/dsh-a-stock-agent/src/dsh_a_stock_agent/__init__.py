from .agent import AShareAgent

# 兼容 main 上 PR #3/#5 的导入名
AStockAgent = AShareAgent

__all__ = ["AShareAgent", "AStockAgent"]
