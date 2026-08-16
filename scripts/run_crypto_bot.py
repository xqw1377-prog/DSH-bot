#!/usr/bin/env python3
"""启动 Crypto Bot：DSH Session + Profile + 真实插件 + 定时运行。

用法：
    python scripts/run_crypto_bot.py --every 60 \
        --gateway http://127.0.0.1:8001 --db ~/.dsh/runtime.db

前提：Quant Gateway 已启动（可用 DSH_LOCAL_PAPER=1 本地纸面模式）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "dsh-runtime" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-crypto-agent" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-quant-gateway" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-trade-approval" / "src"))

from dsh_crypto_agent import CryptoAgent
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, load_profile, run_forever
from dsh_trade_approval import ApprovalWorkflow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://127.0.0.1:8001")
    parser.add_argument("--api-key", default=None, help="Quant Gateway X-API-Key")
    parser.add_argument("--every", type=float, default=60.0, help="tick 间隔秒数")
    parser.add_argument("--account", default="crypto-paper-1")
    parser.add_argument("--min-strength", type=float, default=0.6,
                        help="低于该强度的信号忽略")
    parser.add_argument("--db", default=None, help="记忆/事件 SQLite 路径")
    args = parser.parse_args()

    if args.db:
        import os
        os.environ["DSH_RUNTIME_DB"] = args.db

    profile = load_profile(ROOT / "profiles" / "crypto-bot" / "profile.yaml")
    session = BotSession.for_profile(profile)

    gateway = GatewayClient(base_url=args.gateway)
    approvals = ApprovalWorkflow(
        gateway_base_url=args.gateway, api_key=args.api_key
    )
    agent = CryptoAgent(gateway=gateway, approvals=approvals,
                        account_id=args.account, min_strength=args.min_strength)

    print(f"[dsh] {profile.name} 启动：每 {args.every}s 一个 tick，Ctrl-C 停止")
    run_forever(session, agent, interval_seconds=args.every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
