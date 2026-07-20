from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from .domain import IngestRequest


KNOWN_PREFIXES = ("result_", "url_")


def iter_scratch_assets(root: Path) -> Iterable[tuple[Path, dict]]:
    for meta_path in sorted(root.rglob("_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in meta.get("results", []):
            if item.get("status") != "ok" or not item.get("url"):
                continue
            raw = item.get("scratch_file", "")
            path = (
                Path(raw)
                if raw
                else meta_path.parent / f"result_{item.get('index', 0):03d}.md"
            )
            if not path.is_absolute():
                path = meta_path.parent / path
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                # Old manifests can contain their original absolute location. Only import
                # it if it is the same numbered asset beside the manifest.
                path = meta_path.parent / Path(raw).name
            if path.name.startswith(KNOWN_PREFIXES) and path.is_file():
                yield (
                    path,
                    {
                        **item,
                        "invocation_id": meta.get("invocation_id"),
                        "operation": meta.get("operation"),
                    },
                )


def import_scratch(root: Path, service, dry_run: bool = False) -> dict:
    report = {
        "version": 1,
        "root": str(root),
        "dry_run": dry_run,
        "scanned": 0,
        "imported": 0,
        "reused": 0,
        "failed": 0,
        "items": [],
    }
    for path, item in iter_scratch_assets(root):
        report["scanned"] += 1
        entry = {
            "original_path": str(path),
            "url": item["url"],
            "status": "would_import" if dry_run else "pending",
        }
        try:
            content = path.read_bytes()
            entry["byte_length"] = len(content)
            if not dry_run:
                result = service.ingest(
                    IngestRequest(
                        requested_url=item["url"],
                        content=content,
                        mime_type="application/json"
                        if path.suffix == ".json"
                        else "text/markdown",
                        title=item.get("title"),
                        metadata={
                            "migration": {
                                "original_path": str(path),
                                "invocation_id": item.get("invocation_id"),
                                "operation": item.get("operation"),
                            }
                        },
                    )
                )
                entry.update(
                    {
                        "status": "reused" if result.reused_snapshot else "imported",
                        "source_id": str(result.source_id),
                        "snapshot_id": str(result.snapshot_id),
                        "document_id": str(result.document_id),
                        "content_sha256": result.content_sha256,
                    }
                )
                report[entry["status"]] += 1
        except Exception as exc:
            entry.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            report["failed"] += 1
        report["items"].append(entry)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    return report


def export_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
