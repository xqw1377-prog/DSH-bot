"""向本地内存服务写入演示数据，便于前端联调。"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    try:
        exp_a = post(
            "http://127.0.0.1:8002/v1/experiments",
            {
                "market": "A_SHARE",
                "strategy_id": "mean-reversion-ashare",
                "hypothesis": "高开低走后的日内均值回归在白酒板块仍有效",
                "data_snapshot_id": "paper-data-1",
                "created_by_bot": "a-stock-bot",
            },
        )
        exp_c = post(
            "http://127.0.0.1:8002/v1/experiments",
            {
                "market": "CRYPTO",
                "strategy_id": "funding-basis-crypto",
                "hypothesis": "资金费率极端时基差收敛可覆盖手续费",
                "data_snapshot_id": "paper-data-1",
                "created_by_bot": "crypto-bot",
            },
        )
        cand_a = post(
            "http://127.0.0.1:8002/v1/candidates",
            {
                "market": "A_SHARE",
                "strategy_id": "mean-reversion-ashare",
                "strategy_version": "0.1.0-paper",
            },
        )
        cand_c = post(
            "http://127.0.0.1:8002/v1/candidates",
            {
                "market": "CRYPTO",
                "strategy_id": "funding-basis-crypto",
                "strategy_version": "0.1.0-paper",
            },
        )
        appr = post(
            "http://127.0.0.1:8001/v1/approvals",
            {
                "market": "A_SHARE",
                "requested_by_bot": "market-chief",
                "subject_type": "order",
                "subject_id": "paper-order-preview-600519",
                "evidence_refs": ["paper-signal-1", "paper-risk-1"],
                "binding": {
                    "market": "A_SHARE",
                    "account_id": "paper-a-share-001",
                    "symbol": "600519.SH",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": "100",
                    "strategy_version": "0.1.0-paper",
                    "signal_snapshot_id": "paper-signal-1",
                    "risk_snapshot_id": "paper-risk-1",
                    "valid_until": "2030-01-01T00:00:00Z",
                },
            },
        )
        appr2 = post(
            "http://127.0.0.1:8001/v1/approvals",
            {
                "market": "CRYPTO",
                "requested_by_bot": "crypto-bot",
                "subject_type": "strategy_promotion",
                "subject_id": cand_c["candidate_id"],
                "evidence_refs": ["bt-1", "walkforward-1"],
            },
        )
    except urllib.error.URLError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "experiments": [exp_a["experiment_id"], exp_c["experiment_id"]],
                "candidates": [cand_a["candidate_id"], cand_c["candidate_id"]],
                "approvals": [appr["approval_id"], appr2["approval_id"]],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
