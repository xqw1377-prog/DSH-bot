#!/usr/bin/env python3
"""从 6celue state.json 导出 CRYPTO.json。不读 Streamlit，不下单。"""

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
    parser.add_argument("--state", default=os.environ.get("DSH_CRYPTO_STATE_JSON"))
    parser.add_argument("--out", default=os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR"))
    args = parser.parse_args()
    from dsh_snapshot_bridge import export_crypto_snapshot

    payload = export_crypto_snapshot(
        state_path=Path(args.state) if args.state else None,
        output_dir=Path(args.out) if args.out else None,
    )
    print(
        f"[snapshot] CRYPTO written fresh={payload['data_fresh']} "
        f"degraded={payload['degraded']} account={payload['account_id']}"
    )
    return 0 if payload["data_fresh"] or payload.get("accounts") else 2


if __name__ == "__main__":
    sys.exit(main())
