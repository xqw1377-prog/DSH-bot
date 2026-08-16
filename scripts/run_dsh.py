#!/usr/bin/env python3
"""DSH 多 Bot 运行入口：所有 Bot 经统一的 Profile/Session/Schedule 机制运行。

用法：
    python scripts/run_dsh.py --every 60 \
        --gateway http://127.0.0.1:8001 --db ~/.dsh/runtime.db

启动的 Bot：
- market-chief：跨市场健康汇总 + 待审批待办
- crypto-bot：信号 → 预览 → 人工审批 → Paper 订单（全链路 Paper）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in (
    "packages/dsh-runtime/src",
    "plugins/dsh-quant-gateway/src",
    "plugins/dsh-trade-approval/src",
    "plugins/dsh-crypto-agent/src",
    "plugins/dsh-market-chief/src",
):
    sys.path.insert(0, str(ROOT / sub))

from dsh_crypto_agent import CryptoAgent
from dsh_gateway_client import GatewayClient
from dsh_market_chief import MarketChiefAgent
from dsh_runtime import BotSession, load_profile, run_once
from dsh_trade_approval import ApprovalWorkflow


def build_agents(gateway_url: str, api_key: str | None, account: str,
                 min_strength: float = 0.6):
    gateway = GatewayClient(base_url=gateway_url)
    approvals = ApprovalWorkflow(gateway_base_url=gateway_url, api_key=api_key)
    return [
        (MarketChiefAgent(gateway=gateway), "market-chief"),
        (CryptoAgent(gateway=gateway, approvals=approvals,
                     account_id=account, min_strength=min_strength),
         "crypto-bot"),
    ]


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=os.environ.get(
        "QUANT_GATEWAY_URL", "http://127.0.0.1:8001"
    ))
    parser.add_argument("--api-key", default=os.environ.get("QUANT_GATEWAY_API_KEY"))
    parser.add_argument("--every", type=float, default=60.0)
    parser.add_argument(
        "--account",
        default=(
            os.environ.get("DSH_CRYPTO_ACCOUNT_ID")
            or os.environ.get("PAPER_CRYPTO_ACCOUNT_ID")
            or "paper-crypto-001"
        ),
    )
    parser.add_argument("--min-strength", type=float, default=float(
        os.environ.get("DSH_CRYPTO_MIN_STRENGTH", "0.6")
    ))
    parser.add_argument("--db", default=None, help="记忆/事件/任务 SQLite 路径")
    args = parser.parse_args()

    if args.db:
        os.environ["DSH_RUNTIME_DB"] = args.db

    runners = []
    built = build_agents(args.gateway, args.api_key, args.account,
                         args.min_strength)
    for agent, profile_name in built:
        profile = load_profile(ROOT / "profiles" / profile_name / "profile.yaml")
        runners.append((BotSession.for_profile(profile), agent))

    print(f"[dsh] 启动 {len(runners)} 个 Bot：{[a.name for _, a in runners]}，"
          f"account={args.account} 每 {args.every}s 一轮，Ctrl-C 停止")
    try:
        while True:
            for session, agent in runners:
                run_once(session, agent)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("[dsh] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
