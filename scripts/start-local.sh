#!/usr/bin/env bash
# 本地一键启动完整 Paper 环境（Gateway / Evolution / Risk / Projection / Risk Auditor）。
# 必须提供 .env.local（可从 .env.local.example 复制）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/lib/load_env.sh"

ENV_FILE="${ENV_FILE:-$ROOT/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ROOT/.env.local.example" ]]; then
    cp "$ROOT/.env.local.example" "$ENV_FILE"
    echo "created $ENV_FILE from .env.local.example"
  else
    echo "missing $ENV_FILE" >&2
    exit 1
  fi
fi
load_env "$ENV_FILE"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# 优先用仓库源码，避免 editable 安装漂移
export PYTHONPATH="$ROOT/packages/domain-contracts/src:$ROOT/packages/dsh-runtime/src:$ROOT/services/quant-gateway/src:$ROOT/services/strategy-evolution/src:$ROOT/services/risk-policy/src:$ROOT/services/projection-api/src:$ROOT/plugins/dsh-quant-gateway/src:$ROOT/plugins/dsh-trade-approval/src:$ROOT/plugins/dsh-crypto-agent/src:$ROOT/plugins/dsh-risk-auditor/src:$ROOT/plugins/dsh-market-chief/src${PYTHONPATH:+:$PYTHONPATH}"

export DSH_ENV="${DSH_ENV:-development}"
export DSH_LOCAL_PAPER="${DSH_LOCAL_PAPER:-1}"
export RISK_POLICY_URL="${RISK_POLICY_URL:-http://127.0.0.1:8003}"
export QUANT_GATEWAY_URL="${QUANT_GATEWAY_URL:-http://127.0.0.1:8001}"
export STRATEGY_EVOLUTION_URL="${STRATEGY_EVOLUTION_URL:-http://127.0.0.1:8002}"
export PROJECTION_API_URL="${PROJECTION_API_URL:-http://127.0.0.1:8004}"
export RISK_AUDITOR_URL="${RISK_AUDITOR_URL:-http://127.0.0.1:8005}"
export STRATEGY_EVOLUTION_AUDITOR_URL="${STRATEGY_EVOLUTION_AUDITOR_URL:-$RISK_AUDITOR_URL}"
export QUANT_GATEWAY_DB="${QUANT_GATEWAY_DB:-$ROOT/.data/gateway.db}"
export STRATEGY_EVOLUTION_DB="${STRATEGY_EVOLUTION_DB:-$ROOT/.data/evolution.db}"
export RISK_AUDITOR_DB="${RISK_AUDITOR_DB:-$ROOT/.data/risk-auditor.db}"
export DSH_RUNTIME_DB="${DSH_RUNTIME_DB:-$ROOT/.data/runtime.db}"
export PAPER_CRYPTO_ACCOUNT_ID="${PAPER_CRYPTO_ACCOUNT_ID:-paper-crypto-001}"
export PAPER_A_SHARE_ACCOUNT_ID="${PAPER_A_SHARE_ACCOUNT_ID:-paper-a-share-001}"
export DSH_CRYPTO_ACCOUNT_ID="${DSH_CRYPTO_ACCOUNT_ID:-$PAPER_CRYPTO_ACCOUNT_ID}"

# 相对路径落在仓库根目录
mkdir -p "$ROOT/.data"
for var in QUANT_GATEWAY_DB STRATEGY_EVOLUTION_DB RISK_AUDITOR_DB DSH_RUNTIME_DB; do
  val="${!var}"
  if [[ "$val" != /* && "$val" != :memory:* ]]; then
    export "$var=$ROOT/$val"
  fi
done

pids=()
cleanup() {
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

uvicorn quant_gateway.main:app --host 127.0.0.1 --port 8001 &
pids+=($!)
uvicorn strategy_evolution.main:app --host 127.0.0.1 --port 8002 &
pids+=($!)
uvicorn risk_policy.main:app --host 127.0.0.1 --port 8003 &
pids+=($!)
uvicorn projection_api.main:app --host 127.0.0.1 --port 8004 &
pids+=($!)
uvicorn dsh_risk_auditor.service:app --host 127.0.0.1 --port 8005 &
pids+=($!)

echo "local services starting on 8001-8005"
echo "  paper=$DSH_LOCAL_PAPER env=$DSH_ENV crypto_account=$DSH_CRYPTO_ACCOUNT_ID"
echo "  gateway_db=$QUANT_GATEWAY_DB auditor=$STRATEGY_EVOLUTION_AUDITOR_URL"

# 等待健康检查
for i in 1 2 3 4 5 6 7 8 9 10; do
  ok=1
  for port in 8001 8002 8003 8004 8005; do
    if ! curl -sf "http://127.0.0.1:$port/healthz" >/dev/null; then
      ok=0
      break
    fi
  done
  if [[ "$ok" -eq 1 ]]; then
    echo "all health checks passed"
    wait
    exit 0
  fi
  sleep 0.5
done

echo "services failed health check within timeout" >&2
exit 1
