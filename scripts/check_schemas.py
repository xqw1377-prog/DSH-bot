#!/usr/bin/env python3
"""事件 Schema 覆盖检查 v2。

四类报告，前三类硬失败：
- enum_without_schema：envelope 登记的事件类型没有 payload schema
- schema_without_enum：schema 文件未登记进 envelope enum
- runtime_emitted_without_schema：代码里发射的事件没有 schema（发射时
  Runtime 不会校验，属于契约漏洞）
- schema_never_emitted：schema 存在但没有任何运行时发射方（允许分批
  实现，仅警告）
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "packages" / "event-schemas"
EMITTER_DIRS = [ROOT / "packages", ROOT / "plugins", ROOT / "services"]
EMIT_RE = re.compile(r'\.emit\(\s*"([a-z]+/[a-z_.]+)"', re.S)


def main() -> int:
    enum = set(json.loads((SCHEMAS / "envelope.json").read_text())
               ["properties"]["event_type"]["enum"])

    schemas = {
        str(p.relative_to(SCHEMAS)).removesuffix(".json")
        for p in SCHEMAS.rglob("*.json") if p.name != "envelope.json"
    }

    emitted: set[str] = set()
    for d in EMITTER_DIRS:
        for py in d.rglob("*.py"):
            if "egg-info" in str(py) or ".venv" in str(py):
                continue
            emitted |= set(EMIT_RE.findall(py.read_text()))

    failures = []
    warnings = []

    for name, items in (
        ("enum_without_schema", sorted(enum - schemas)),
        ("schema_without_enum", sorted(schemas - enum)),
        ("runtime_emitted_without_schema", sorted(emitted - schemas)),
        ("emitted_not_in_enum", sorted(emitted - enum)),
    ):
        if items:
            failures.append((name, items))
    never = schemas - emitted
    if never:
        warnings.append(("schema_never_emitted", sorted(never)))

    for name, items in failures:
        print(f"FAIL {name} ({len(items)}):")
        for i in items:
            print(f"  - {i}")
    for name, items in warnings:
        print(f"WARN {name} ({len(items)}):")
        for i in items:
            print(f"  - {i}")

    if failures:
        print(f"FAILED: {sum(len(i) for _, i in failures)} violations")
        return 1
    print(f"OK: enum={len(enum)} schemas={len(schemas)} "
          f"runtime_emitted={len(emitted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
