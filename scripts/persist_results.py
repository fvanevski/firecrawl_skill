#!/usr/bin/env python3
"""Persist one explicit wrapper manifest; never scans a scratch directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from research_store.compat import export_json
from research_store.container import build_service
from research_store.domain import IngestRequest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("meta")
    parser.add_argument("--output", required=True)
    parser.add_argument("--research-run-id")
    args = parser.parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        return 0
    meta_path = Path(args.meta)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    service = build_service()
    assets = []
    expected_successes = 0
    for item in meta.get("results", []):
        asset_metadata = {
            "firecrawl": {
                "invocation_id": meta.get("invocation_id"),
                "operation": meta.get("operation"),
                "scratch_compat_path": item.get("scratch_file"),
                "result_index": item.get("index"),
                "acquisition_status": item.get("status"),
            }
        }
        if item.get("status") != "ok":
            assets.append(
                {
                    "requested_url": item.get("url") or "unknown:",
                    "error": item.get("error") or "Firecrawl acquisition failed",
                    "metadata": asset_metadata,
                }
            )
            continue
        expected_successes += 1
        try:
            path = Path(item["scratch_file"])
            raw_value = item.get("raw_scratch_file")
            raw_path = Path(raw_value) if raw_value else path
            assets.append(
                {
                    "request": IngestRequest(
                        requested_url=item["url"],
                        content=raw_path.read_bytes(),
                        normalized_content=path.read_bytes(),
                        mime_type=(
                            "application/json"
                            if path.suffix == ".json"
                            else "text/markdown"
                        ),
                        title=item.get("title"),
                        metadata=asset_metadata,
                    ),
                    "metadata": asset_metadata,
                }
            )
        except Exception as exc:
            assets.append(
                {
                    "requested_url": item.get("url") or "unknown:",
                    "error": f"{type(exc).__name__}: {exc}",
                    "metadata": {**asset_metadata, "persistence_input_failed": True},
                }
            )

    try:
        result = service.persist_manifest_batch(
            meta,
            assets,
            research_run_external_id=args.research_run_id,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {
            "version": 2,
            "invocation_id": meta.get("invocation_id"),
            "operation": meta.get("operation"),
            "status": "failed",
            "error": error,
            "failure_count": len(assets),
            "assets": [
                {
                    "ordinal": ordinal,
                    "requested_url": (
                        item["request"].requested_url
                        if item.get("request") is not None
                        else item.get("requested_url", "unknown:")
                    ),
                    "status": "failed",
                    "error": item.get("error") or error,
                    "metadata": item.get("metadata", {}),
                }
                for ordinal, item in enumerate(assets)
            ],
        }
        export_json(Path(args.output), result)
        print(json.dumps(result, default=str))
        return 1
    result["version"] = 2
    export_json(Path(args.output), result)
    print(json.dumps(result, default=str))
    retained_successes = sum(
        item.get("status") == "complete" for item in result.get("assets", [])
    )
    return 1 if retained_successes != expected_successes else 0


if __name__ == "__main__":
    raise SystemExit(main())
