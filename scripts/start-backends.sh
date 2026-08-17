#!/usr/bin/env bash
# 生产安全启动脚本：要求 API Key，禁止开发开放模式，默认不启用 Paper。
# 本地联调请使用 scripts/start-local.sh。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

ENV_MODE="${DSH_ENV:-production}"
if [[ "$ENV_MODE" == "development" ]]; then
  echo "refusing to start: start-backends.sh is production-safe; DSH_ENV=development is not allowed." >&2
  echo "use scripts/start-local.sh for local Paper development." >&2
  exit 1
fi

if [[ -z "${QUANT_GATEWAY_API_KEYS:-}" ]]; then
  echo "refusing to start: QUANT_GATEWAY_API_KEYS is required (fail-closed)." >&2
  exit 1
fi

if [[ "${DSH_LOCAL_PAPER:-0}" == "1" ]]; then
  echo "refusing to start: DSH_LOCAL_PAPER=1 is not allowed in start-backends.sh." >&2
  exit 1
fi

if [[ -z "${STRATEGY_EVOLUTION_AUDITOR_URL:-${RISK_AUDITOR_URL:-}}" ]]; then
  echo "refusing to start: STRATEGY_EVOLUTION_AUDITOR_URL (or RISK_AUDITOR_URL) is required." >&2
  exit 1
fi

export DSH_ENV=production
export DSH_LOCAL_PAPER=0
export RISK_POLICY_URL="${RISK_POLICY_URL:-http://127.0.0.1:8003}"
export QUANT_GATEWAY_URL="${QUANT_GATEWAY_URL:-http://127.0.0.1:8001}"
export STRATEGY_EVOLUTION_URL="${STRATEGY_EVOLUTION_URL:-http://127.0.0.1:8002}"
export RISK_AUDITOR_URL="${RISK_AUDITOR_URL:-http://127.0.0.1:8005}"
export STRATEGY_EVOLUTION_AUDITOR_URL="${STRATEGY_EVOLUTION_AUDITOR_URL:-$RISK_AUDITOR_URL}"
export QUANT_GATEWAY_DB="${QUANT_GATEWAY_DB:-$ROOT/.data/gateway.db}"
export STRATEGY_EVOLUTION_DB="${STRATEGY_EVOLUTION_DB:-$ROOT/.data/evolution.db}"
export RISK_AUDITOR_DB="${RISK_AUDITOR_DB:-$ROOT/.data/risk-auditor.db}"
export DSH_RUNTIME_DB="${DSH_RUNTIME_DB:-$ROOT/.data/runtime.db}"
# Projection / Bot 服务端持有，浏览器不持有
export QUANT_GATEWAY_API_KEY="${QUANT_GATEWAY_API_KEY:-}"
mkdir -p "$ROOT/.data"

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

echo "production backends on 8001-8005 (paper=off env=$DSH_ENV)"
wait
