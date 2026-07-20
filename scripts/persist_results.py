#!/usr/bin/env python3
"""Persist one explicit wrapper manifest; never scans a scratch directory."""

from __future__ import annotations

import argparse
from dataclasses import asdict
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
    args = parser.parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        return 0
    meta_path = Path(args.meta)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    service = build_service()
    result = {"version": 1, "invocation_id": meta.get("invocation_id"), "assets": []}
    for item in meta.get("results", []):
        if item.get("status") != "ok":
            continue
        path = Path(item["scratch_file"])
        raw_path = Path(item.get("raw_scratch_file", path))
        ingest = service.ingest(
            IngestRequest(
                requested_url=item["url"],
                content=raw_path.read_bytes(),
                normalized_content=path.read_bytes(),
                mime_type="application/json"
                if path.suffix == ".json"
                else "text/markdown",
                title=item.get("title"),
                metadata={
                    "firecrawl": {
                        "invocation_id": meta.get("invocation_id"),
                        "operation": meta.get("operation"),
                        "scratch_compat_path": str(path),
                    }
                },
            )
        )
        serialized = asdict(ingest)
        for key in ("source_id", "snapshot_id", "document_id"):
            serialized[key] = str(serialized[key])
        serialized["chunk_ids"] = [str(value) for value in serialized["chunk_ids"]]
        serialized["url"] = item["url"]
        result["assets"].append(serialized)
    export_json(Path(args.output), result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
