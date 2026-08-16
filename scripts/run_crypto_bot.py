#!/usr/bin/env python3
"""启动 Crypto Bot：DSH Session + Profile + 真实插件 + 定时运行。

账户 ID 来自统一配置（DSH_CRYPTO_ACCOUNT_ID / PAPER_CRYPTO_ACCOUNT_ID），
启动时向 Gateway 校验账户存在且市场匹配；错误账户直接失败退出。

用法：
    python scripts/run_crypto_bot.py --every 60

前提：本地可用 scripts/start-local.sh 启动后端（含 Paper）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "dsh-runtime" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-crypto-agent" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-quant-gateway" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-trade-approval" / "src"))

from dsh_contracts import Market
from dsh_crypto_agent import CryptoAgent
from dsh_gateway_client import GatewayClient, GatewayError
from dsh_runtime import BotSession, load_profile, run_forever
from dsh_trade_approval import ApprovalWorkflow


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def resolve_account_id(cli_account: str | None) -> str:
    if cli_account:
        return cli_account
    return (
        os.environ.get("DSH_CRYPTO_ACCOUNT_ID")
        or os.environ.get("PAPER_CRYPTO_ACCOUNT_ID")
        or "paper-crypto-001"
    )


def resolve_market() -> Market:
    raw = os.environ.get("DSH_CRYPTO_MARKET", "CRYPTO")
    return Market(raw)


def validate_account(gateway: GatewayClient, account_id: str, market: Market) -> None:
    try:
        summaries = gateway.get_account_summary(market)
    except GatewayError as exc:
        raise SystemExit(
            f"account validation failed: cannot read accounts from gateway: {exc}"
        ) from exc

    match = next((s for s in summaries if s.get("account_id") == account_id), None)
    if match is None:
        known = [s.get("account_id") for s in summaries]
        raise SystemExit(
            f"account validation failed: account_id={account_id!r} not found "
            f"for market={market.value}; known={known}"
        )
    reported = match.get("market")
    if reported and reported != market.value:
        raise SystemExit(
            f"account validation failed: account_id={account_id!r} market "
            f"mismatch expected={market.value} got={reported}"
        )


def main() -> int:
    _load_dotenv(ROOT / ".env.local")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=os.environ.get(
        "QUANT_GATEWAY_URL", "http://127.0.0.1:8001"
    ))
    parser.add_argument("--api-key", default=os.environ.get("QUANT_GATEWAY_API_KEY"))
    parser.add_argument(
        "--every",
        type=float,
        default=float(os.environ.get("DSH_CRYPTO_TICK_SECONDS", "60")),
        help="tick 间隔秒数",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="覆盖 DSH_CRYPTO_ACCOUNT_ID（须与 Paper 配置一致）",
    )
    parser.add_argument(
        "--min-strength",
        type=float,
        default=float(os.environ.get("DSH_CRYPTO_MIN_STRENGTH", "0.6")),
        help="低于该强度的信号忽略",
    )
    parser.add_argument("--db", default=None, help="记忆/事件 SQLite 路径")
    parser.add_argument(
        "--once", action="store_true", help="只跑一个 tick 后退出（冒烟用）"
    )
    parser.add_argument(
        "--skip-account-check",
        action="store_true",
        help="跳过启动账户校验（仅测试）",
    )
    args = parser.parse_args()

    if args.db:
        os.environ["DSH_RUNTIME_DB"] = args.db
    elif "DSH_RUNTIME_DB" in os.environ and not os.environ["DSH_RUNTIME_DB"].startswith(
        ("/", ":")
    ):
        os.environ["DSH_RUNTIME_DB"] = str(ROOT / os.environ["DSH_RUNTIME_DB"])

    account_id = resolve_account_id(args.account)
    market = resolve_market()

    profile = load_profile(ROOT / "profiles" / "crypto-bot" / "profile.yaml")
    session = BotSession.for_profile(profile)

    gateway = GatewayClient(base_url=args.gateway)
    if not args.skip_account_check:
        validate_account(gateway, account_id, market)

    approvals = ApprovalWorkflow(
        gateway_base_url=args.gateway, api_key=args.api_key
    )
    agent = CryptoAgent(
        gateway=gateway,
        approvals=approvals,
        account_id=account_id,
        min_strength=args.min_strength,
    )

    print(
        f"[dsh] {profile.name} 启动：account={account_id} market={market.value} "
        f"every={args.every}s"
    )
    if args.once:
        from dsh_runtime import run_once

        run_once(session, agent)
        return 0

    run_forever(session, agent, interval_seconds=args.every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
