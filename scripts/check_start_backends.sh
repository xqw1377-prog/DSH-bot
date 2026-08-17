#!/usr/bin/env bash
# 生产启动脚本必须拒绝 development / Paper。不真正拉起服务。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

expect_refuse() {
  local name="$1"
  shift
  set +e
  "$@" >/tmp/dsh-start-backends-check.txt 2>&1
  local rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    echo "[check] $name: expected refuse, script succeeded" >&2
    cat /tmp/dsh-start-backends-check.txt >&2
    exit 1
  fi
  echo "[check] $name refused as expected"
}

expect_refuse "development" env DSH_ENV=development bash "$ROOT/scripts/start-backends.sh"
expect_refuse "paper" env DSH_ENV=production QUANT_GATEWAY_API_KEYS="k/n:read,write" \
  DSH_LOCAL_PAPER=1 bash "$ROOT/scripts/start-backends.sh"
expect_refuse "missing keys" env DSH_ENV=production QUANT_GATEWAY_API_KEYS= \
  bash "$ROOT/scripts/start-backends.sh"

if ! grep -q 'filter @dsh-bot/client-sdk build' \
  "$ROOT/infra/containers/apps/dsh-bot-web.Dockerfile"; then
  echo "[check] web Dockerfile missing client-sdk build" >&2
  exit 1
fi
echo "[check] web Dockerfile builds client-sdk"
echo "[check] PASS"
