#!/usr/bin/env bash
# 加载 KEY=VALUE 环境文件（支持 # 注释与空行；不执行任意 shell）。
# 用法: source scripts/lib/load_env.sh && load_env /path/to/.env.local

load_env() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "missing env file: $file" >&2
    return 1
  fi
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      # 去掉成对引号
      if [[ "$value" =~ ^\"(.*)\"$ ]]; then
        value="${BASH_REMATCH[1]}"
      elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "$key=$value"
    else
      echo "invalid env line in $file: $line" >&2
      return 1
    fi
  done < "$file"
}
