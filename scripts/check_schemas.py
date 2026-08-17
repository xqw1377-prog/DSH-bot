#!/usr/bin/env python3
"""校验事件 schema 目录与 envelope.json 的 event_type enum 一致。

规则：每个 payload schema 文件 `<dir>/<name>.json` 必须对应 envelope
enum 中的 `<dir>/<name>`；envelope 中的事件类型若尚无 payload schema，
给出警告（允许分批补齐）。
"""

import json
import sys
from pathlib import Path

SCHEMAS = Path(__file__).resolve().parent.parent / "packages" / "event-schemas"


def main() -> int:
    envelope = json.loads((SCHEMAS / "envelope.json").read_text())
    enum = set(envelope["properties"]["event_type"]["enum"])

    files = {
        str(p.relative_to(SCHEMAS)).removesuffix(".json")
        for p in SCHEMAS.rglob("*.json")
        if p.name != "envelope.json"
    }

    orphan = files - enum
    if orphan:
        print("ERROR: schema files not registered in envelope event_type enum:")
        for name in sorted(orphan):
            print(f"  - {name}")
        return 1

    missing = enum - files
    if missing:
        print("ERROR: event types without payload schema (must add schema):")
        for name in sorted(missing):
            print(f"  - {name}")
        return 1

    print(f"OK: {len(files)} payload schemas consistent with envelope ({len(enum)} event types)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
