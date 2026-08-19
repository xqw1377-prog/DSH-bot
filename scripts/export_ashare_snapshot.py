#!/usr/bin/env python3
"""从 ZISU wallet/screen API 导出 A_SHARE.json。API 失败关闭，不读数据库。"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", default=os.environ.get("DSH_A_SHARE_WALLET_URL"))
    parser.add_argument("--screen", default=os.environ.get("DSH_A_SHARE_SCREEN_URL"))
    parser.add_argument("--out", default=os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR"))
    args = parser.parse_args()
    from dsh_snapshot_bridge import export_ashare_snapshot

    payload = export_ashare_snapshot(
        wallet_url=args.wallet,
        screen_url=args.screen,
        output_dir=Path(args.out) if args.out else None,
    )
    print(
        f"[snapshot] A_SHARE written fresh={payload['data_fresh']} "
        f"degraded={payload['degraded']} account={payload['account_id']}"
    )
    return 0 if payload["data_fresh"] or payload.get("accounts") else 2


if __name__ == "__main__":
    sys.exit(main())
