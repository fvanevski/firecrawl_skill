from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .domain import utcnow


def _export_json(path: Path, payload: Any) -> None:
    """Write JSON atomically via temp-file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class CompatibilityExportResult:
    export_id: UUID | None
    run_id: UUID
    search_response_id: UUID
    target_dir: Path
    source_state_sha256: str
    status: str
    files_created: list[Path] = field(default_factory=list)
    error: str | None = None


class SearchCompatibilityExporter:
    """Derives search compatibility exports (_search.json, _candidates.json, _meta.json) from PostgreSQL authority."""

    def __init__(self, uow_factory: Callable, blob_store: Any | None = None):
        self.uow_factory = uow_factory
        self.blob_store = blob_store

    def export_search(
        self,
        run_id: UUID,
        search_response_id: UUID,
        target_dir: Path | str,
        *,
        idempotency_key: str | None = None,
        export_schema_version: int = 1,
    ) -> CompatibilityExportResult:
        run_id = UUID(str(run_id))
        search_response_id = UUID(str(search_response_id))
        target_dir = Path(target_dir)
        key = idempotency_key or f"export:search:{search_response_id}"

        store = self.blob_store
        if store is None:
            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )

        resp_data = None
        raw_bytes = b""
        occurrences = []

        with self.uow_factory() as uow:
            resp_data = uow.runs.get_search_response(search_response_id, run_id=run_id)
            sha256_digest = resp_data["raw_blob_sha256"]
            with store.open(sha256_digest) as handle:
                raw_bytes = handle.read()

            candidates = uow.runs.list_candidates(run_id)

            for cand in candidates:
                cand_occs = uow.runs.list_candidate_occurrences(cand["id"], run_id=run_id)
                for occ in cand_occs:
                    if str(occ["search_response_id"]) == str(search_response_id):
                        occurrences.append(
                            {
                                "candidate": cand,
                                "occurrence": occ,
                            }
                        )

        occurrences.sort(
            key=lambda item: (item["occurrence"]["rank"], str(item["candidate"]["id"]))
        )

        candidate_items = []
        for item in occurrences:
            cand = item["candidate"]
            occ = item["occurrence"]
            candidate_items.append(
                {
                    "candidate_id": str(cand["id"]),
                    "canonical_url": cand["canonical_url"],
                    "original_url": cand["original_url"],
                    "rank": occ["rank"],
                    "query_text": occ["query_text"],
                    "domain": cand["domain"],
                    "title": cand.get("title"),
                    "snippet": cand.get("snippet"),
                    "recurrence_count": cand["recurrence_count"],
                    "duplicate_group_id": str(cand["duplicate_group_id"])
                    if cand.get("duplicate_group_id")
                    else None,
                    "date_signals": cand.get("date_signals", {}),
                    "backend_metadata": cand.get("backend_metadata", {}),
                }
            )

        source_state_str = (
            raw_bytes.decode("utf-8", errors="replace")
            + json.dumps(candidate_items, sort_keys=True)
        )
        source_state_sha256 = hashlib.sha256(
            source_state_str.encode("utf-8")
        ).hexdigest()

        created_at_val = resp_data.get("created_at")
        created_at_str = (
            created_at_val.isoformat()
            if hasattr(created_at_val, "isoformat")
            else str(created_at_val)
        )

        meta_payload = {
            "query": resp_data["query_text"],
            "search_response_id": str(resp_data["id"]),
            "run_id": str(resp_data["run_id"]),
            "plan_id": str(resp_data["plan_id"]) if resp_data.get("plan_id") else None,
            "plan_query_id": str(resp_data["plan_query_id"])
            if resp_data.get("plan_query_id")
            else None,
            "backend": resp_data["backend"],
            "status": resp_data["status"],
            "candidate_count": len(candidate_items),
            "source_state_sha256": source_state_sha256,
            "export_schema_version": export_schema_version,
            "created_at": created_at_str,
            "generated_at": utcnow().isoformat(),
        }

        files_created = []
        export_status = "complete"
        export_err = None
        export_id = None

        try:
            target_dir.mkdir(parents=True, exist_ok=True)

            search_json_path = target_dir / "_search.json"
            temp_search = search_json_path.with_name(f".{search_json_path.name}.tmp")
            temp_search.write_bytes(raw_bytes)
            temp_search.replace(search_json_path)
            files_created.append(search_json_path)

            candidates_json_path = target_dir / "_candidates.json"
            _export_json(
                candidates_json_path,
                {
                    "search_response_id": str(resp_data["id"]),
                    "run_id": str(resp_data["run_id"]),
                    "candidate_count": len(candidate_items),
                    "candidates": candidate_items,
                },
            )
            files_created.append(candidates_json_path)

            meta_json_path = target_dir / "_meta.json"
            _export_json(meta_json_path, meta_payload)
            files_created.append(meta_json_path)

        except Exception as exc:
            export_status = "failed"
            export_err = f"{type(exc).__name__}: {exc}"

        try:
            with self.uow_factory() as uow:
                export_id = uow.runs.record_compatibility_export(
                    run_id,
                    "search_compat",
                    export_schema_version,
                    source_state_sha256,
                    export_status,
                    key,
                    filesystem_path=str(target_dir),
                    error=export_err,
                    metadata=meta_payload,
                )
                uow.commit()
        except Exception:
            pass

        return CompatibilityExportResult(
            export_id=export_id,
            run_id=run_id,
            search_response_id=search_response_id,
            target_dir=target_dir,
            source_state_sha256=source_state_sha256,
            status=export_status,
            files_created=files_created,
            error=export_err,
        )

    def regenerate_search_exports(
        self, run_id: UUID, target_dir: Path | str
    ) -> list[CompatibilityExportResult]:
        """Regenerate search compatibility exports for all search responses of a run."""
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        results = []
        with self.uow_factory() as uow:
            responses = uow.runs.list_search_responses(run_id)

        for idx, resp in enumerate(responses, start=1):
            sub_dir = target_dir / f"response_{idx:03d}"
            res = self.export_search(run_id, resp["id"], sub_dir)
            results.append(res)
        return results
