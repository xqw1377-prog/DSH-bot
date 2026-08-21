#!/usr/bin/env python3
"""把 packages/event-schemas 同步进 dsh-runtime 包内(wheel 安装备份路径)。

CI/打包时调用;源码树(editable)布局不需要。同步是全量镜像,
以源码树为准,防止两份漂移。
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "packages" / "event-schemas"
DST = ROOT / "packages" / "dsh-runtime" / "src" / "dsh_runtime" / "event-schemas"


def main() -> int:
    if not SRC.is_dir():
        print(f"source schemas not found: {SRC}", file=sys.stderr)
        return 1
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    count = sum(1 for _ in DST.rglob("*.json"))
    print(f"synced {count} schema files -> {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
