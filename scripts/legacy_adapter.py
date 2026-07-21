#!/usr/bin/env python3
"""CLI bridge used by legacy wrappers at their completed-decision boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from research_store.container import build_legacy_adapter
from research_store.legacy_adapter import AdapterMode, LegacyAdapterError
from research_store.service import dumps


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="legacy-adapter")
    result.add_argument(
        "--mode",
        default=os.environ.get("FIRECRAWL_LEGACY_ADAPTER_MODE", "compatibility"),
    )
    result.add_argument(
        "--entry-point", choices=("frun", "fsearch_smart", "fsearch", "fscrape"), required=True
    )
    result.add_argument("--action", required=True)
    result.add_argument("--status", choices=("pending", "running", "complete", "failed"), required=True)
    result.add_argument("--metadata")
    result.add_argument("--research-run-id")
    result.add_argument("--invocation-id")
    result.add_argument("--idempotency-key", required=True)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        mode = AdapterMode.parse(args.mode)
        metadata = {}
        if args.metadata:
            metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        decision = {
            "action": args.action,
            "status": args.status,
            "input": metadata,
        }
        result = build_legacy_adapter(mode).route(
            args.entry_point,
            decision,
            external_run_id=args.research_run_id,
            external_invocation_id=args.invocation_id,
            idempotency_key=args.idempotency_key,
        )
    except (OSError, ValueError, json.JSONDecodeError, LegacyAdapterError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    print(dumps(result.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
