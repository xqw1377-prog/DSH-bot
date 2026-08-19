#!/usr/bin/env python3
"""同时导出 CRYPTO / A_SHARE 只读快照。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "dsh-snapshot-bridge" / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def main() -> int:
    _load_dotenv(ROOT / ".env.shadow")
    _load_dotenv(ROOT / ".env.local")
    from dsh_snapshot_bridge import export_ashare_snapshot, export_crypto_snapshot

    crypto = export_crypto_snapshot()
    ashare = export_ashare_snapshot()
    print(
        f"[snapshot] dual crypto_fresh={crypto['data_fresh']} "
        f"ashare_fresh={ashare['data_fresh']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
