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
    run_forever(cycle, base_interval=max(args.every, 60))


def run_forever(cycle, *, base_interval: float, sleep=None, log=None) -> None:
    """24h 无人值守主循环:单轮异常绝不杀死循环。

    连续失败按 1x→2x→...→10x 退避(上限 base_interval 的 10 倍),
    成功后恢复正常间隔;每轮输出 status/duration/failures 便于观测。
    sleep/log 可注入以便测试。
    """
    do_sleep = sleep or time.sleep
    emit = log or (lambda payload: print(json.dumps(payload, ensure_ascii=False), flush=True))
    base_interval = max(base_interval, 1.0)
    interval = base_interval
    failures = 0
    while True:
        started = time.monotonic()
        try:
            result = cycle()
            failures = 0
            interval = base_interval
            result["_loop"] = {
                "status": "ok",
                "duration_seconds": round(time.monotonic() - started, 1),
                "consecutive_failures": 0,
            }
            emit(result)
            do_sleep(interval)
        except KeyboardInterrupt:
            raise
        except StopLoop:
            return
        except Exception as exc:  # noqa: BLE001 循环守护:记录并继续
            failures += 1
            interval = min(base_interval * min(failures, 10), base_interval * 10)
            emit({
                "_loop": {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": round(time.monotonic() - started, 1),
                    "consecutive_failures": failures,
                    "next_interval_seconds": interval,
                },
            })
            try:
                do_sleep(interval)
            except StopLoop:
                return


class StopLoop(Exception):
    """测试辅助:由注入的 sleep 抛出以结束循环。"""


if __name__ == "__main__":
    sys.exit(main())
