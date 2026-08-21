from __future__ import annotations

import argparse
import json

from intelligence_ingest.pipeline import ingest_once
from intelligence_ingest.registry import load_registry, x_filter_rules


def main() -> None:
    parser = argparse.ArgumentParser(description="DSH 只读情报采集")
    parser.add_argument("command", choices=["ingest", "sources", "x-rules"])
    parser.add_argument("--derived", action="store_true", help="同时拉 GitHub Release / 项目 RSS")
    args = parser.parse_args()
    if args.command == "sources":
        registry = load_registry()
        print(json.dumps({"enabled": [s.id for s in registry.enabled_sources()]}, ensure_ascii=False))
        return
    if args.command == "x-rules":
        print(json.dumps(x_filter_rules(load_registry()), ensure_ascii=False, indent=2))
        return
    result = ingest_once(include_derived=args.derived)
    print(json.dumps({k: v for k, v in result.items() if k != "events_preview"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
