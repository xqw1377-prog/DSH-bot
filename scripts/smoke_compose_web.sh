#!/usr/bin/env bash
# Compose 浏览器级冒烟：只打 Next BFF，不直连 Projection 内网地址。
# 需要先启动：docker compose -f infra/containers/docker-compose.yml up -d
# 以及本地开发：pnpm --filter dsh-bot-web dev
set -euo pipefail
WEB="${DSH_WEB_URL:-http://127.0.0.1:3000}"

echo "[web-smoke] csrf"
CSRF=$(curl -sf "$WEB/api/csrf")
printf '%s' "$CSRF" | python3 -c "import json,sys; t=json.load(sys.stdin); assert t.get('csrf_token'), t"

echo "[web-smoke] projection BFF incidents"
curl -sf "$WEB/api/projection/v1/incidents" | python3 -c "import json,sys; rows=json.load(sys.stdin); assert isinstance(rows, list), rows"

echo "[web-smoke] projection BFF tasks"
curl -sf "$WEB/api/projection/v1/bot-tasks" | python3 -c "import json,sys; rows=json.load(sys.stdin); assert isinstance(rows, list), rows"

echo "[web-smoke] PASS"
