#!/usr/bin/env bash
# P0 进程级冒烟：健康检查 → 错误账户失败 → Crypto 审批/下单/成交 → 审计门禁 → 重启恢复。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/lib/load_env.sh"

ENV_FILE="${ENV_FILE:-$ROOT/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.local.example" "$ENV_FILE"
fi
load_env "$ENV_FILE"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export PYTHONPATH="$ROOT/packages/domain-contracts/src:$ROOT/packages/dsh-runtime/src:$ROOT/services/quant-gateway/src:$ROOT/services/strategy-evolution/src:$ROOT/services/risk-policy/src:$ROOT/services/projection-api/src:$ROOT/services/incident-center/src:$ROOT/plugins/dsh-quant-gateway/src:$ROOT/plugins/dsh-trade-approval/src:$ROOT/plugins/dsh-crypto-agent/src:$ROOT/plugins/dsh-a-stock-agent/src:$ROOT/plugins/dsh-risk-auditor/src:$ROOT/plugins/dsh-market-chief/src${PYTHONPATH:+:$PYTHONPATH}"

SMOKE_DIR="${SMOKE_DIR:-$ROOT/.data/smoke}"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"

export DSH_ENV=development
export DSH_LOCAL_PAPER=1
export QUANT_GATEWAY_DB="$SMOKE_DIR/gateway.db"
export STRATEGY_EVOLUTION_DB="$SMOKE_DIR/evolution.db"
export RISK_AUDITOR_DB="$SMOKE_DIR/risk-auditor.db"
export DSH_RUNTIME_DB="$SMOKE_DIR/runtime.db"
export RISK_POLICY_URL=http://127.0.0.1:8003
export QUANT_GATEWAY_URL=http://127.0.0.1:8001
export STRATEGY_EVOLUTION_URL=http://127.0.0.1:8002
export PROJECTION_API_URL=http://127.0.0.1:8004
export RISK_AUDITOR_URL=http://127.0.0.1:8005
export STRATEGY_EVOLUTION_AUDITOR_URL=http://127.0.0.1:8005
export PAPER_CRYPTO_ACCOUNT_ID="${PAPER_CRYPTO_ACCOUNT_ID:-paper-crypto-001}"
export PAPER_A_SHARE_ACCOUNT_ID="${PAPER_A_SHARE_ACCOUNT_ID:-paper-a-share-001}"
export DSH_CRYPTO_ACCOUNT_ID="${DSH_CRYPTO_ACCOUNT_ID:-$PAPER_CRYPTO_ACCOUNT_ID}"
export DSH_A_SHARE_ACCOUNT_ID="${DSH_A_SHARE_ACCOUNT_ID:-$PAPER_A_SHARE_ACCOUNT_ID}"
export DSH_CRYPTO_MARKET=CRYPTO
export DSH_CRYPTO_MIN_STRENGTH=0.6

PIDS=()
AUDITOR_PID=""

cleanup() {
  if [[ -n "$AUDITOR_PID" ]]; then kill "$AUDITOR_PID" 2>/dev/null || true; fi
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

start_core() {
  uvicorn quant_gateway.main:app --host 127.0.0.1 --port 8001 &
  PIDS+=($!)
  uvicorn strategy_evolution.main:app --host 127.0.0.1 --port 8002 &
  PIDS+=($!)
  uvicorn risk_policy.main:app --host 127.0.0.1 --port 8003 &
  PIDS+=($!)
  uvicorn projection_api.main:app --host 127.0.0.1 --port 8004 &
  PIDS+=($!)
}

start_auditor() {
  uvicorn dsh_risk_auditor.service:app --host 127.0.0.1 --port 8005 &
  AUDITOR_PID=$!
}

wait_health() {
  local need_auditor="${1:-1}"
  local i port
  for i in $(seq 1 40); do
    local ok=1
    for port in 8001 8002 8003 8004; do
      if ! curl -sf "http://127.0.0.1:$port/healthz" >/dev/null; then
        ok=0
        break
      fi
    done
    if [[ "$need_auditor" == "1" ]]; then
      if ! curl -sf "http://127.0.0.1:8005/healthz" >/dev/null; then
        ok=0
      fi
    fi
    if [[ "$ok" -eq 1 ]]; then
      echo "[smoke] health ok"
      return 0
    fi
    sleep 0.25
  done
  echo "[smoke] health check failed" >&2
  return 1
}

stop_all() {
  if [[ -n "$AUDITOR_PID" ]]; then kill "$AUDITOR_PID" 2>/dev/null || true; wait "$AUDITOR_PID" 2>/dev/null || true; AUDITOR_PID=""; fi
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  for p in "${PIDS[@]:-}"; do wait "$p" 2>/dev/null || true; done
  PIDS=()
  sleep 0.4
}

start_core
start_auditor
wait_health 1

echo "[smoke] wrong account must fail at startup"
set +e
python scripts/run_crypto_bot.py --once --account definitely-wrong-account >/tmp/dsh-smoke-wrong.txt 2>&1
rc=$?
set -e
if [[ "$rc" -eq 0 ]]; then
  echo "[smoke] expected account validation failure" >&2
  cat /tmp/dsh-smoke-wrong.txt >&2
  exit 1
fi
grep -q "account validation failed" /tmp/dsh-smoke-wrong.txt

echo "[smoke] crypto tick → one approval"
python scripts/run_crypto_bot.py --once
APPROVALS=$(curl -sf "$QUANT_GATEWAY_URL/v1/approvals?status=REQUESTED")
COUNT=$(printf '%s' "$APPROVALS" | python -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$COUNT" != "1" ]]; then
  echo "[smoke] expected exactly 1 approval, got $COUNT" >&2
  echo "$APPROVALS" >&2
  exit 1
fi
APPROVAL_ID=$(printf '%s' "$APPROVALS" | python -c "import json,sys; print(json.load(sys.stdin)[0]['approval_id'])")

python scripts/run_crypto_bot.py --once
COUNT2=$(curl -sf "$QUANT_GATEWAY_URL/v1/approvals" | python -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$COUNT2" != "1" ]]; then
  echo "[smoke] duplicate approval detected: $COUNT2" >&2
  exit 1
fi

echo "[smoke] approve → submit → fill"
curl -sf -X POST "$QUANT_GATEWAY_URL/v1/approvals/$APPROVAL_ID/decide" \
  -H 'content-type: application/json' \
  -d '{"decision":"APPROVED","decided_by":"smoke"}' >/dev/null
python scripts/run_crypto_bot.py --once

ORDER_ID=$(curl -sf "$QUANT_GATEWAY_URL/v1/audit" | python -c "
import json,sys
rows=json.load(sys.stdin)
ids=[r['subject_id'] for r in rows if r.get('action')=='order.submitted']
assert len(ids)==1, ids
print(ids[0])
")
STATUS=$(curl -sf "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/orders/$ORDER_ID")
printf '%s' "$STATUS" | python -c "import json,sys; s=json.load(sys.stdin); assert s['status']=='FILLED', s; print('filled', s['order_id'])"

python - <<PY
import os, sqlite3
db = os.environ["DSH_RUNTIME_DB"]
conn = sqlite3.connect(db)
rows = conn.execute("SELECT status, reconciliation_status FROM bot_tasks WHERE bot='crypto-bot'").fetchall()
assert rows, "no bot tasks"
assert all(r[0] == "DONE" for r in rows), rows
assert all((r[1] or "") in ("MATCHED",) for r in rows), rows
print("tasks done and matched", len(rows))
PY

echo "[smoke] projection tasks + incidents + chief refuse"
curl -sf "$PROJECTION_API_URL/v1/bot-tasks" | python -c "
import json,sys
rows=json.load(sys.stdin)
assert rows, 'no projected tasks'
assert all(r['status']=='DONE' for r in rows), rows
assert all(r.get('reconciliation_status')=='MATCHED' for r in rows), rows
print('projection tasks', len(rows))
"
curl -sf "$PROJECTION_API_URL/v1/incidents" | python -c "
import json,sys
rows=json.load(sys.stdin)
assert isinstance(rows, list)
print('incidents', len(rows))
"
curl -sf -X POST "$PROJECTION_API_URL/v1/chief/query" \
  -H 'content-type: application/json' \
  -d '{"question":"请你立刻批准"}' | python -c "
import json,sys
body=json.load(sys.stdin)
assert body.get('refused') is True, body
print('chief refused action')
"
curl -sf -X POST "$PROJECTION_API_URL/v1/chief/query" \
  -H 'content-type: application/json' \
  -d '{"question":"现在系统健康吗"}' | python -c "
import json,sys
body=json.load(sys.stdin)
assert body.get('refused') is False, body
assert '任务' in body.get('text',''), body
print('chief health ok')
"

echo "[smoke] live mode refused"
set +e
python scripts/run_crypto_bot.py --once --mode live >/tmp/dsh-smoke-live.txt 2>&1
live_rc=$?
set -e
if [[ "$live_rc" -eq 0 ]]; then
  echo "[smoke] live mode must fail" >&2
  cat /tmp/dsh-smoke-live.txt >&2
  exit 1
fi
grep -q "live mode is disabled" /tmp/dsh-smoke-live.txt

echo "[smoke] a-share paper closeout"
python scripts/run_a_stock_bot.py --once
ASHARE_APPROVALS=$(curl -sf "$QUANT_GATEWAY_URL/v1/approvals?status=REQUESTED&market=A_SHARE")
ASHARE_COUNT=$(printf '%s' "$ASHARE_APPROVALS" | python -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$ASHARE_COUNT" != "1" ]]; then
  echo "[smoke] expected 1 a-share approval, got $ASHARE_COUNT" >&2
  echo "$ASHARE_APPROVALS" >&2
  exit 1
fi
ASHARE_ID=$(printf '%s' "$ASHARE_APPROVALS" | python -c "import json,sys; print(json.load(sys.stdin)[0]['approval_id'])")
curl -sf -X POST "$QUANT_GATEWAY_URL/v1/approvals/$ASHARE_ID/decide" \
  -H 'content-type: application/json' \
  -d '{"decision":"APPROVED","decided_by":"smoke"}' >/dev/null
python scripts/run_a_stock_bot.py --once
python - <<PY
import os, sqlite3
conn = sqlite3.connect(os.environ["DSH_RUNTIME_DB"])
rows = conn.execute("SELECT status, reconciliation_status FROM bot_tasks WHERE bot='a-stock-bot'").fetchall()
assert rows, "no a-share tasks"
assert all(r[0] == "DONE" for r in rows), rows
assert all((r[1] or "") == "MATCHED" for r in rows), rows
print("a-share tasks done and matched", len(rows))
PY

echo "[smoke] auditor unavailable → cannot promote"
CAND=$(curl -sf -X POST "$STRATEGY_EVOLUTION_URL/v1/candidates" \
  -H 'content-type: application/json' \
  -d '{"market":"CRYPTO","strategy_id":"smoke-s","strategy_version":"0.1.0"}')
CAND_ID=$(printf '%s' "$CAND" | python -c "import json,sys; print(json.load(sys.stdin)['candidate_id'])")
for stage_refs in 'BACKTESTED|["b1"]' 'VALIDATED|["b1","p1"]' 'PAPER|["b1","p1","s1"]' 'SHADOW|["b1","p1","s1"]'; do
  stage="${stage_refs%%|*}"
  refs="${stage_refs#*|}"
  curl -sf -X POST "$STRATEGY_EVOLUTION_URL/v1/candidates/$CAND_ID/promote" \
    -H 'content-type: application/json' \
    -d "{\"target_stage\":\"$stage\",\"evidence_refs\":$refs}" >/dev/null
done

kill "$AUDITOR_PID" 2>/dev/null || true
wait "$AUDITOR_PID" 2>/dev/null || true
AUDITOR_PID=""
sleep 0.5
CODE=$(curl -s -o /tmp/dsh-promote.json -w '%{http_code}' -X POST \
  "$STRATEGY_EVOLUTION_URL/v1/candidates/$CAND_ID/promote" \
  -H 'content-type: application/json' \
  -d '{"target_stage":"APPROVED","evidence_refs":["b1","p1","s1"],"approval_id":"appr-smoke"}')
if [[ "$CODE" != "503" ]]; then
  echo "[smoke] expected 503 fail-closed promote, got $CODE" >&2
  cat /tmp/dsh-promote.json >&2
  exit 1
fi
grep -qi 'unreachable\|fail-closed' /tmp/dsh-promote.json

echo "[smoke] restart → approvals/orders/memory recoverable"
stop_all
start_core
start_auditor
wait_health 1

curl -sf "$QUANT_GATEWAY_URL/v1/approvals/$APPROVAL_ID" | python -c "import json,sys; a=json.load(sys.stdin); assert a['status']=='APPROVED', a"
curl -sf "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/orders/$ORDER_ID" | python -c "import json,sys; s=json.load(sys.stdin); assert s['status']=='FILLED', s"

python scripts/run_crypto_bot.py --once
COUNT3=$(curl -sf "$QUANT_GATEWAY_URL/v1/approvals?market=CRYPTO" | python -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$COUNT3" != "1" ]]; then
  echo "[smoke] memory not recovered; crypto approvals=$COUNT3" >&2
  exit 1
fi

echo "[smoke] kill switch request/succeed/resume"
curl -sf -X POST "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/emergency-stop" >/dev/null
curl -sf "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/health" | python -c "
import json,sys
h=json.load(sys.stdin)
assert h.get('system_ok') is False, h
assert h.get('trading_channel_ok') is False, h
print('kill switch halted', h.get('detail'))
"
curl -sf -X POST "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/kill-switch/resume" >/dev/null
curl -sf "$QUANT_GATEWAY_URL/v1/markets/CRYPTO/health" | python -c "
import json,sys
h=json.load(sys.stdin)
assert h.get('system_ok') is True, h
assert h.get('trading_channel_ok') is True, h
print('kill switch resumed')
"
curl -sf "$QUANT_GATEWAY_URL/v1/audit" | python -c "
import json,sys
actions={r.get('action') for r in json.load(sys.stdin)}
assert 'kill_switch.requested' in actions, actions
assert 'kill_switch.succeeded' in actions, actions
assert 'kill_switch.resumed' in actions, actions
print('kill switch audit ok')
"
curl -sf "$PROJECTION_API_URL/v1/incidents" | python -c "
import json,sys
rows=json.load(sys.stdin)
assert any(r.get('event_type','').startswith('kill_switch/') for r in rows), rows
print('kill switch projected', sum(1 for r in rows if str(r.get('event_type','')).startswith('kill_switch/')))
"

echo "[smoke] shadow records decision without new approval"
python scripts/run_crypto_bot.py --once --mode shadow --db "$SMOKE_DIR/runtime-shadow.db"
COUNT4=$(curl -sf "$QUANT_GATEWAY_URL/v1/approvals?market=CRYPTO" | python -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$COUNT4" != "1" ]]; then
  echo "[smoke] shadow created approval; crypto approvals=$COUNT4" >&2
  exit 1
fi
python - <<PY
import sqlite3
conn = sqlite3.connect("$SMOKE_DIR/runtime-shadow.db")
rows = conn.execute("SELECT status FROM bot_tasks").fetchall()
assert rows, "no shadow tasks"
assert all(r[0] == "SHADOW_RECORDED" for r in rows), rows
print("shadow recorded", len(rows))
PY

echo "[smoke] PASS"
