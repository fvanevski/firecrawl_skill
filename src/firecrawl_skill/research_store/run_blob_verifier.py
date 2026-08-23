"""Run-scoped verification of authoritative content-addressed payloads.

PostgreSQL decides which immutable BLOB_ROOT objects belong to a research run.
The blob store proves whether those exact bytes still exist and match their
declared SHA-256 digest. Derived chunk ``content_sha256`` values are
intentionally out of scope: they hash chunk text and are not BLOB_ROOT keys.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

_PAGE_SIZE = 100


@dataclass(frozen=True)
class BlobReference:
    """One logical run-owned reference to an immutable blob."""

    source: str
    record_id: str
    field: str
    sha256: str
    path: str | None = None

    def to_dict(self, *, state: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "record_id": self.record_id,
            "field": self.field,
            "sha256": self.sha256,
            "state": state,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.source == "legacy_invocation_output":
            payload["invocation_id"] = self.record_id
        return payload


def _repository(uow: Any, name: str) -> Any | None:
    try:
        return getattr(uow, name)
    except AttributeError:
        return None


def _postgres_blob_references(uow: Any, run_id: UUID) -> list[BlobReference]:
    references: list[BlobReference] = []

    searches = _repository(uow, "search_responses")
    if searches is not None:
        for row in searches.list_search_responses(run_id):
            digest = row.get("raw_blob_sha256")
            if digest:
                references.append(
                    BlobReference(
                        source="search_response",
                        record_id=str(row["id"]),
                        field="raw_blob_sha256",
                        sha256=str(digest),
                    )
                )

    attempts = _repository(uow, "extraction_attempts")
    if attempts is not None:
        offset = 0
        while True:
            rows = attempts.list_attempts_for_run(
                run_id,
                limit=_PAGE_SIZE,
                offset=offset,
            )
            for row in rows:
                for field in ("raw_blob_sha256", "normalized_blob_sha256"):
                    digest = row.get(field)
                    if digest:
                        references.append(
                            BlobReference(
                                source="extraction_attempt",
                                record_id=str(row["id"]),
                                field=field,
                                sha256=str(digest),
                            )
                        )
            if len(rows) < _PAGE_SIZE:
                break
            offset += len(rows)

    snapshots = _repository(uow, "snapshots")
    snapshot_reader = cast(
        Callable[..., list[dict[str, Any]]] | None,
        getattr(snapshots, "list_run_blob_references", None),
    )
    if callable(snapshot_reader):
        offset = 0
        while True:
            rows = snapshot_reader(run_id, limit=_PAGE_SIZE, offset=offset)
            for row in rows:
                digest = row.get("content_sha256")
                if digest:
                    references.append(
                        BlobReference(
                            source="asset_snapshot",
                            record_id=str(row["id"]),
                            field="content_sha256",
                            sha256=str(digest),
                            path=(
                                str(row["raw_blob_uri"])
                                if row.get("raw_blob_uri")
                                else None
                            ),
                        )
                    )
            if len(rows) < _PAGE_SIZE:
                break
            offset += len(rows)

    return references


def _legacy_references(
    invocations: list[dict[str, Any]],
) -> tuple[list[BlobReference], list[dict[str, Any]]]:
    references: list[BlobReference] = []
    file_only: list[dict[str, Any]] = []

    def collect(invocation_id: object, artifact: object) -> None:
        if isinstance(artifact, dict):
            path = artifact.get("path")
            digest = artifact.get("sha256")
            if path and digest:
                references.append(
                    BlobReference(
                        source="legacy_invocation_output",
                        record_id=str(invocation_id),
                        field="sha256",
                        sha256=str(digest),
                        path=str(path),
                    )
                )
            elif path:
                file_only.append(
                    {
                        "invocation_id": str(invocation_id),
                        "source": "legacy_invocation_output",
                        "path": str(path),
                        "state": "file_based_unverified",
                    }
                )
        elif isinstance(artifact, list):
            for item in artifact:
                collect(invocation_id, item)
        elif isinstance(artifact, str):
            file_only.append(
                {
                    "invocation_id": str(invocation_id),
                    "source": "legacy_invocation_output",
                    "path": artifact,
                    "state": "file_based_unverified",
                }
            )

    for invocation in invocations:
        output = invocation.get("output") or {}
        for result in output.get("results", []):
            for key in ("snapshot", "artifacts"):
                artifact = result.get(key)
                if artifact is not None:
                    collect(invocation["id"], artifact)

    return references, file_only


def collect_run_blob_evidence(
    uow: Any,
    run_id: UUID,
) -> tuple[list[BlobReference], list[dict[str, Any]]]:
    """Collect every schema-backed run BLOB reference plus legacy file evidence."""

    invocations = uow.runs.list_invocations(run_id)
    postgres = _postgres_blob_references(uow, run_id)
    legacy, file_only = _legacy_references(invocations)
    return postgres + legacy, file_only


def verify_run_blobs(
    uow_factory: Any,
    blob_store: Any,
    run_id: UUID,
) -> dict[str, Any]:
    """Verify logical run references against the configured content-addressed store."""

    with uow_factory() as uow:
        references, file_only = collect_run_blob_evidence(uow, run_id)

    states_by_sha: dict[str, str] = {}
    artifacts: list[dict[str, Any]] = []
    available = 0
    missing = 0
    hash_mismatch = 0

    for reference in references:
        state = states_by_sha.get(reference.sha256)
        if state is None:
            exists = (
                getattr(blob_store, "exists", None) if blob_store is not None else None
            )
            verify = (
                getattr(blob_store, "verify", None) if blob_store is not None else None
            )
            if (
                blob_store is None
                or not callable(exists)
                or not exists(reference.sha256)
            ):
                state = "missing"
            elif not callable(verify) or not verify(reference.sha256):
                state = "hash_mismatch"
            else:
                state = "available"
            states_by_sha[reference.sha256] = state

        if state == "available":
            available += 1
        elif state == "missing":
            missing += 1
        else:
            hash_mismatch += 1
        artifacts.append(reference.to_dict(state=state))

    artifacts.extend(file_only)
    total = len(references)
    if total == 0:
        status = "inconclusive"
    elif missing or hash_mismatch:
        status = "failed"
    else:
        status = "passed"

    return {
        "target": str(run_id),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "total": total,
        "unique_blobs": len(states_by_sha),
        "available": available,
        "missing": missing,
        "hash_mismatch": hash_mismatch,
        "file_based_unverified": len(file_only),
        "artifacts": artifacts,
    }


__all__ = [
    "BlobReference",
    "collect_run_blob_evidence",
    "verify_run_blobs",
]
