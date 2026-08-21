#!/usr/bin/env bash
# Shadow 冒烟：快照导出 → 只读 Gateway → 写路径 403 → 两 Bot 不下单。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export PYTHONPATH="$ROOT/packages/domain-contracts/src:$ROOT/packages/dsh-runtime/src:$ROOT/packages/dsh-snapshot-bridge/src:$ROOT/services/quant-gateway/src:$ROOT/services/projection-api/src:$ROOT/services/risk-policy/src:$ROOT/plugins/dsh-quant-gateway/src:$ROOT/plugins/dsh-trade-approval/src:$ROOT/plugins/dsh-crypto-agent/src:$ROOT/plugins/dsh-a-stock-agent/src:$ROOT/plugins/dsh-market-chief/src${PYTHONPATH:+:$PYTHONPATH}"

SMOKE="${SMOKE_DIR:-$ROOT/.data/shadow-smoke}"
rm -rf "$SMOKE"
mkdir -p "$SMOKE/snapshots" "$SMOKE/src"

export DSH_ENV=development
export DSH_LOCAL_PAPER=0
export QUANT_GATEWAY_READ_ONLY=1
export QUANT_GATEWAY_SNAPSHOT_DIR="$SMOKE/snapshots"
export QUANT_GATEWAY_API_KEYS="shadow-read/shadow-reader:read;shadow-write/shadow-writer:read,write"
export QUANT_GATEWAY_API_KEY="shadow-read"
export QUANT_GATEWAY_URL=http://127.0.0.1:8011
export QUANT_GATEWAY_DB="$SMOKE/gateway.db"
export DSH_RUNTIME_DB="$SMOKE/runtime.db"
export DSH_CRYPTO_MODE=shadow
export DSH_A_SHARE_MODE=shadow
export DSH_CRYPTO_ACCOUNT_ID=paper-crypto-001
export DSH_A_SHARE_ACCOUNT_ID=paper-a-share-001
export PAPER_CRYPTO_ACCOUNT_ID=paper-crypto-001
export PAPER_A_SHARE_ACCOUNT_ID=paper-a-share-001
export DSH_CRYPTO_SOURCE_SYSTEM=6celue_v5
export DSH_CRYPTO_SOURCE_MODE=demo
export DSH_A_SHARE_SOURCE_SYSTEM=zisu
export DSH_A_SHARE_SOURCE_MODE=paper
export DSH_SNAPSHOT_STALE_SECONDS=3600

python - <<PY
import json
from datetime import UTC, datetime
from pathlib import Path
import os

smoke = Path(os.environ["QUANT_GATEWAY_SNAPSHOT_DIR"]).parent
state = {
    "last_update": datetime.now(UTC).timestamp(),
    "dry_run": True,
    "balance": {
        "total_balance": "1000.50",
        "available_balance": "400.25",
        "margin_used": "600.25",
        "sync_ok": True,
    },
    "exchange_positions": {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.10",
            "entry_price": "67000.00",
        }
    },
}
state_path = smoke / "src" / "state.json"
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state), encoding="utf-8")
(state_path.parent / "signals.jsonl").write_text(
    json.dumps(
        {
            "client_order_id": "smoke-crypto-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "action": "pending",
            "signal_type": "V5",
            "signal_tier": "S",
            "timestamp": datetime.now(UTC).isoformat(),
            "entry_price": "67000.00",
        }
    )
    + "\n",
    encoding="utf-8",
)
print(state_path)
PY

export DSH_CRYPTO_STATE_JSON="$SMOKE/src/state.json"

# 本地 HTTP 给 A 股 API
python - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

WALLET = {
    "updated_at": "2026-08-18T02:00:00+00:00",
    "cash_balance": "1048340",
    "total_asset": "1250000",
    "quote_health": {"ok": True},
    "positions": [
        {
            "symbol": "600519.SH",
            "quantity": 120,
            "sellable_quantity": 120,
            "t_plus_one_locked": 0,
            "avg_cost": "1680.50",
        }
    ],
    "recent_trades": [],
}
SCREEN = {"actionable": [{"symbol": "600519.SH", "policy_action": "buy", "executable": True, "engine_id": "leaf"}]}
COCKPIT = {
    "policy_decisions": {
        "_decision_event_id": "smoke-evt",
        "contract_version": "zisu-smoke",
        "quotes_updated_at": "2026-08-18T02:00:00+00:00",
        "decisions": [
            {
                "symbol": "600519.SH",
                "action": "buy",
                "executable": True,
                "engine_id": "leaf",
                "thesis_id": "smoke",
                "evidence_score": 0.8,
                "reasons": ["smoke"],
                "leaf_signal": {"confidence": 0.8},
            }
        ],
    }
}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/wallet"):
            body = WALLET
        elif self.path.endswith("/cockpit"):
            body = COCKPIT
        else:
            body = SCREEN
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        return

HTTPServer(("127.0.0.1", 8769), H).serve_forever()
PY
ZISU_PID=$!
trap 'kill $ZISU_PID ${PIDS[@]:-} 2>/dev/null || true' EXIT
sleep 0.3

export DSH_A_SHARE_WALLET_URL=http://127.0.0.1:8769/api/paper/wallet
export DSH_A_SHARE_SCREEN_URL=http://127.0.0.1:8769/api/trade/screen

python scripts/export_dual_quant_snapshots.py

python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["QUANT_GATEWAY_SNAPSHOT_DIR"])
crypto = json.loads((root / "CRYPTO.json").read_text(encoding="utf-8"))
ashare = json.loads((root / "A_SHARE.json").read_text(encoding="utf-8"))
assert crypto["account_id"] == "paper-crypto-001"
assert ashare["account_id"] == "paper-a-share-001"
assert ashare["positions"][0]["symbol"] == "600519"
assert ashare["positions"][0]["source_symbol"] == "600519.SH"
assert crypto["signals"] and crypto["signals"][0]["signal_id"] == "smoke-crypto-1"
assert ashare["signals"] and ashare["signals"][0]["symbol"] == "600519"
assert ashare["screen_results"][0]["kind"] == "SCREEN_RESULT"
assert all(row.get("kind") != "SCREEN_RESULT" for row in ashare["signals"])
assert crypto["health"]["trading_channel_ok"] is False
print("snapshot fixtures ok")
PY

PIDS=()
uvicorn quant_gateway.main:app --host 127.0.0.1 --port 8011 &
PIDS+=($!)
for i in $(seq 1 30); do
  if curl -sf -H "X-API-Key: shadow-read" http://127.0.0.1:8011/healthz >/dev/null; then
    break
  fi
  sleep 0.2
done

code=$(curl -s -o /tmp/shadow-write.json -w "%{http_code}" -H "X-API-Key: shadow-read" \
  -H "Content-Type: application/json" \
  -d '{"market":"CRYPTO"}' \
  http://127.0.0.1:8011/v1/markets/CRYPTO/emergency-stop)
if [[ "$code" != "403" ]]; then
  echo "expected 403 emergency-stop, got $code" >&2
  exit 1
fi

code=$(curl -s -o /tmp/shadow-order.json -w "%{http_code}" -H "X-API-Key: shadow-write" \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key":"x","market":"CRYPTO","account_id":"paper-crypto-001","strategy_id":"s","strategy_version":"1","symbol":"BTCUSDT","side":"BUY","quantity":"1","valid_until":"2026-08-19T00:00:00+00:00","signal_snapshot_id":"s","risk_snapshot_id":"r"}' \
  http://127.0.0.1:8011/v1/markets/CRYPTO/orders)
if [[ "$code" != "403" ]]; then
  echo "expected 403 request_order even with write scope, got $code" >&2
  exit 1
fi

code=$(curl -s -o /tmp/shadow-cancel.json -w "%{http_code}" -H "X-API-Key: shadow-write" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8011/v1/markets/CRYPTO/orders/x/cancel)
if [[ "$code" != "403" ]]; then
  echo "expected 403 cancel even with write scope, got $code" >&2
  exit 1
fi

code=$(curl -s -o /tmp/shadow-ks.json -w "%{http_code}" -H "X-API-Key: shadow-write" \
  -X POST \
  http://127.0.0.1:8011/v1/markets/CRYPTO/emergency-stop)
if [[ "$code" != "403" ]]; then
  echo "expected 403 emergency-stop even with write scope, got $code" >&2
  exit 1
fi

for i in 1 2 3; do
  python scripts/run_crypto_bot.py --gateway http://127.0.0.1:8011 --api-key shadow-read --mode shadow --once
  python scripts/run_a_stock_bot.py --gateway http://127.0.0.1:8011 --api-key shadow-read --mode shadow --once
done
python - <<'PY'
from pathlib import Path
import sys
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "plugins" / "dsh-market-chief" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "dsh-quant-gateway" / "src"))
from dsh_gateway_client import GatewayClient
from dsh_market_chief import MarketChiefAgent
from dsh_runtime import BotSession, load_profile, run_once
gateway = GatewayClient(base_url="http://127.0.0.1:8011", api_key="shadow-read")
profile = load_profile(ROOT / "profiles" / "market-chief" / "profile.yaml")
run_once(BotSession.for_profile(profile), MarketChiefAgent(gateway=gateway))
print("chief briefing tick ok")
PY
if python scripts/run_crypto_bot.py --mode live; then
  echo "live must fail" >&2
  exit 1
fi

python - <<'PY'
import os, sqlite3
from pathlib import Path
gw = sqlite3.connect(os.environ["QUANT_GATEWAY_DB"])
def count(conn, sql):
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
approvals = count(gw, "select count(*) from approvals")
orders = count(gw, "select count(*) from paper_orders")
cancels = count(gw, "select count(*) from audit_log where action like '%cancel%'")
writes = count(
    gw,
    "select count(*) from audit_log where action in "
    "('order.submitted','order.cancelled','approval.decided','emergency.stop')"
)
assert approvals == 0, approvals
assert orders == 0, orders
assert cancels == 0, cancels
assert writes == 0, writes
rt = sqlite3.connect(os.environ["DSH_RUNTIME_DB"])
shadow = rt.execute(
    "select bot, subject_id, count(*) from bot_tasks"
    " where status='SHADOW_RECORDED' group by bot, subject_id"
).fetchall()
assert shadow, shadow
bots = {row[0] for row in shadow}
assert "crypto-bot" in bots and "a-stock-bot" in bots, shadow
assert all(row[2] == 1 for row in shadow), shadow
brief = rt.execute(
    "select count(*) from agent_memory where bot='market-chief' and kind='daily-briefing'"
).fetchone()[0]
assert brief >= 1, brief
print("shadow smoke ok", shadow)
PY
