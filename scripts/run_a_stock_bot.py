#!/usr/bin/env python3
"""启动 A 股 Bot：复用 TradeExecutionCore，账户来自统一环境变量。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "dsh-runtime" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-a-stock-agent" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-quant-gateway" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-trade-approval" / "src"))

from dsh_a_stock_agent import AShareAgent
from dsh_contracts import Market
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
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def resolve_account_id(cli_account: str | None) -> str:
    if cli_account:
        return cli_account
    value = (
        os.environ.get("DSH_A_SHARE_ACCOUNT_ID")
        or os.environ.get("PAPER_A_SHARE_ACCOUNT_ID")
    )
    if not value:
        raise SystemExit(
            "account validation failed: set DSH_A_SHARE_ACCOUNT_ID or PAPER_A_SHARE_ACCOUNT_ID"
        )
    return value


def validate_account(gateway: GatewayClient, account_id: str) -> None:
    try:
        summaries = gateway.get_account_summary(Market.A_SHARE)
    except GatewayError as exc:
        raise SystemExit(
            f"account validation failed: cannot read accounts from gateway: {exc}"
        ) from exc
    match = next((s for s in summaries if s.get("account_id") == account_id), None)
    if match is None:
        known = [s.get("account_id") for s in summaries]
        raise SystemExit(
            f"account validation failed: account_id={account_id!r} not found "
            f"for market=A_SHARE; known={known}"
        )


def main() -> int:
    _load_dotenv(ROOT / ".env.local")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=os.environ.get(
        "QUANT_GATEWAY_URL", "http://127.0.0.1:8001"
    ))
    parser.add_argument("--api-key", default=os.environ.get("QUANT_GATEWAY_API_KEY"))
    parser.add_argument("--every", type=float, default=60)
    parser.add_argument("--account", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--mode",
        default=os.environ.get("DSH_A_SHARE_MODE", "paper"),
        choices=("paper", "shadow", "live"),
    )
    parser.add_argument("--skip-account-check", action="store_true")
    args = parser.parse_args()
    if args.mode == "live":
        raise SystemExit(
            "live mode is disabled until a real venue adapter, "
            "single-writer store, identity, and outbox are complete"
        )
    if args.db:
        os.environ["DSH_RUNTIME_DB"] = args.db

    account_id = resolve_account_id(args.account)
    profile = load_profile(ROOT / "profiles" / "a-stock-bot" / "profile.yaml")
    session = BotSession.for_profile(profile)
    gateway = GatewayClient(base_url=args.gateway, api_key=args.api_key)
    if not args.skip_account_check:
        validate_account(gateway, account_id)
    approvals = ApprovalWorkflow(
        gateway_base_url=args.gateway, api_key=args.api_key
    )
    agent = AShareAgent(
        gateway=gateway, approvals=approvals, account_id=account_id, mode=args.mode,
    )
    print(f"[dsh] {profile.name} 启动：account={account_id} mode={args.mode}")
    if args.once:
        from dsh_runtime import run_once
        run_once(session, agent)
        return 0
    run_forever(session, agent, interval_seconds=args.every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
