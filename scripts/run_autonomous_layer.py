#!/usr/bin/env python3
"""主动智能层：定时采集权威源、形成 Shadow、跟踪复盘、写日报。

不要和 15 秒快照导出循环合并。默认 5 分钟一轮。
live 不可选。不审批、不下单。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "dsh-runtime" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "domain-contracts" / "src"))
sys.path.insert(0, str(ROOT / "services" / "intelligence-ingest" / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def _ingest(include_derived: bool) -> dict:
    from intelligence_ingest.pipeline import ingest_once

    return ingest_once(include_derived=include_derived)


def main() -> int:
    _load_dotenv(ROOT / ".env.shadow")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=float, default=300, help="唤醒间隔秒，默认 300")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-ingest", action="store_true", help="只跑 Shadow/审计/成交回填，不访问公网源")
    parser.add_argument("--derived", action="store_true", help="同时拉 GitHub/项目 RSS")
    args = parser.parse_args()
    if os.environ.get("DSH_CRYPTO_MODE") == "live" or os.environ.get("DSH_A_SHARE_MODE") == "live":
        raise SystemExit("autonomous layer refuses live")

    from dsh_runtime.autonomous import run_autonomous_cycle

    def cycle() -> dict:
        return run_autonomous_cycle(
            profiles_root=ROOT / "profiles",
            ingest=None if args.no_ingest else lambda: _ingest(args.derived),
            snapshot_dir=os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR"),
        )

    if args.once:
        print(json.dumps(cycle(), ensure_ascii=False, indent=2))
        return 0
    while True:
        print(json.dumps(cycle(), ensure_ascii=False), flush=True)
        time.sleep(max(args.every, 60))


if __name__ == "__main__":
    sys.exit(main())
