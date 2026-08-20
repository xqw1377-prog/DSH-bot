#!/usr/bin/env bash
# Harness Spike 验证脚本（在锁定的 harness checkout 中运行）。
# 用法：bash verify.sh <harness-checkout-path>
#   harness checkout 必须已 checkout 到 SPIKE_COMMIT 并完成 pnpm install + build:lib
set -euo pipefail

HARNESS="${1:?usage: verify.sh <harness-checkout>}"
SPIKE_COMMIT="47f943859bef"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cd "$HARNESS"
ACTUAL=$(git rev-parse --short=12 HEAD)
if [ "$ACTUAL" != "${SPIKE_COMMIT:0:12}" ]; then
  echo "FAIL: harness HEAD $ACTUAL != pinned $SPIKE_COMMIT"
  exit 1
fi
echo "OK: pinned commit $ACTUAL"

# 渲染补丁：插件路径必须是绝对路径（loader 相对路径按 profile home 解析）
sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
  "$REPO_ROOT/tools/harness-spike/cordis.yml" > /tmp/spike-cordis.yml

echo "--- dump-config（验证插件进入启动树）---"
DSH_EXAMPLE_MODE=src pnpm dsh --profile headless \
  --patch /tmp/spike-cordis.yml --dump-config > /tmp/spike-dump.txt 2>/tmp/spike-dump.err || {
    echo "FAIL: dump-config exited nonzero"; tail -20 /tmp/spike-dump.err; exit 1; }
grep -q "dsh-chief-readonly" /tmp/spike-dump.txt \
  && echo "OK: plugin row present in booted tree" \
  || { echo "FAIL: plugin row missing"; exit 1; }
grep -q "\[dsh-chief-readonly\] loaded" /tmp/spike-dump.err \
  && echo "OK: plugin lifecycle apply() ran at boot" \
  || echo "WARN: lifecycle log not captured in dump mode (loader may defer plugin start)"

echo "--- keyless 完整回合（llm-replay 驱动，调用只读审计工具）---"
RUNTIME=$(mktemp -d)
cp "$REPO_ROOT/tools/harness-spike/replay/session.jsonl" \
   "$REPO_ROOT/tools/harness-spike/replay/replay.override.json" "$RUNTIME/"
sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" -e "s|__RUNTIME_DIR__|$RUNTIME|g" \
  "$REPO_ROOT/tools/harness-spike/replay/turn.yml" > /tmp/spike-turn.yml
# 隔离 DSH_HOME，避免污染用户会话存储
export DSH_HOME="$(mktemp -d)"
OUT=$(DSH_EXAMPLE_MODE=src pnpm dsh --profile headless \
        --patch /tmp/spike-turn.yml "run the readonly audit" 2>&1) || {
  echo "FAIL: keyless turn exited nonzero"; echo "$OUT" | tail -10; exit 1; }
echo "$OUT" | grep -q "CHIEF_READONLY_OK" \
  && echo "OK: full turn completed with readonly audit tool call" \
  || { echo "FAIL: expected CHIEF_READONLY_OK in output"; echo "$OUT"; exit 1; }
SESS=$(find "$DSH_HOME/sessions" -name "session.jsonl.zst*" 2>/dev/null | head -1)
if [ -n "$SESS" ]; then
  zstd -dc "$SESS" 2>/dev/null | grep -q "readonly_guaranteed" \
    && echo "OK: tool/result persisted in session log (readonly_guaranteed present)" \
    || echo "WARN: session log tool/result not found at $SESS"
fi

echo "Spike verification complete (structural + keyless full turn)."
