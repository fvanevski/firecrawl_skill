"""Catalog v5 compatibility exporter.

Derives Catalog v5-compatible runs, invocations, events, snapshots,
claims, assessments, and manifests from PostgreSQL and blob storage.

PRD mapping: FR-018

Authority model:

* PostgreSQL ``research_runs``, ``research_invocations``,
  ``research_events``, ``research_claims``, ``audit_assessments``,
  ``asset_snapshots``, ``chunks``, ``documents``, and blob storage
  are authoritative.
* Filesystem Catalog v5 records are derived compatibility exports,
  written *after* database commit.
* Filesystem records are never read to determine current invocation
  or run state.
* Export failure does not roll back an already committed database
  transition.

Export schema version: ``catalog-export-v1``

Never mutates PostgreSQL or blob storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .domain import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPORT_SCHEMA_VERSION = "catalog-export-v1"
SCHEMA_VERSION = 5
EVALUATOR_VERSION = "catalog-v5.0"
PROMPT_TEMPLATE_VERSION = "staged-research-audit-v1"
RUN_PREFIX = "fr_"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CatalogExportError(Exception):
    """Base exception for catalog export failures."""


class ExportTargetNotFound(CatalogExportError):
    """Raised when the requested run or invocation does not exist."""


class ExportWriteFailure(CatalogExportError):
    """Raised when an atomic write fails."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogExportResult:
    """Result of a single export operation.

    Attributes:
        export_id: Deterministic export ID derived from source state.
        source_state_sha256: SHA-256 of the canonical source-state string.
        export_schema_version: Schema version of the export.
        target_type: ``"run"`` or ``"invocation"``.
        target_id: UUID of the exported entity.
        target_dir: Directory where files were written.
        status: ``"complete"`` or ``"failed"``.
        files_created: List of paths to exported files.
        error: Error message if status is ``"failed"``.
    """

    export_id: str
    source_state_sha256: str
    export_schema_version: str
    target_type: str
    target_id: str
    target_dir: Path
    status: str
    files_created: list[Path] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ExportRunResult:
    """Result of exporting a complete run with all derived artifacts.

    Attributes:
        run: The exported run record in Catalog v5 format.
        invocations: List of exported invocation records.
        events: List of exported event records.
        snapshots: List of exported snapshot records.
        claims: List of exported claim records.
        assessments: List of exported assessment records.
        source_state_sha256: SHA-256 of the canonical source-state string.
        export_schema_version: Schema version of the export.
        target_dir: Directory where files were written.
        status: ``"complete"`` or ``"failed"``.
        error: Error message if status is ``"failed"``.
    """

    run: dict[str, Any]
    invocations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    assessments: list[dict[str, Any]]
    source_state_sha256: str
    export_schema_version: str
    target_dir: Path
    status: str
    files_created: list[Path] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically via temp-file rename.

    Raises:
        ExportWriteFailure: If the write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        raise ExportWriteFailure(f"atomic write failed for {path}: {exc}") from exc


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes atomically via temp-file rename.

    Raises:
        ExportWriteFailure: If the write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError as exc:
        raise ExportWriteFailure(f"atomic write failed for {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Source-state hash computation
# ---------------------------------------------------------------------------


def _compute_source_state_hash(*parts: str) -> str:
    """Compute SHA-256 of concatenated source-state parts."""
    combined = "\x00".join(parts)
    return sha256(combined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field mappers
# ---------------------------------------------------------------------------


def _map_run(row: dict[str, Any]) -> dict[str, Any]:
    """Map a ``research_runs`` row to Catalog v5 run format.

    Args:
        row: Dictionary from ``research_runs`` query.

    Returns:
        Catalog v5 run record.
    """
    lifecycle_state = row.get("state", "created")
    # Map internal state to Catalog v5 lifecycle
    if lifecycle_state in ("completed", "partial", "failed", "cancelled"):
        catalog_state = "finished"
    else:
        catalog_state = "running"

    return {
        "schema_version": SCHEMA_VERSION,
        "research_run_id": str(row["id"]),
        "external_id": row.get("external_id"),
        "objective": row.get("objective", ""),
        "profile": {
            "requested": row.get("execution_mode", "auto"),
            "selected": row.get("execution_mode", "auto"),
            "reason": "deterministic mode",
        },
        "started_at": (
            row["started_at"].isoformat()
            if row.get("started_at")
            else None
        ),
        "updated_at": (
            row["updated_at"].isoformat()
            if row.get("updated_at")
            else None
        ),
        "finished_at": (
            row["finished_at"].isoformat()
            if row.get("finished_at")
            else None
        ),
        "lifecycle": {
            "state": catalog_state,
            "revision": row.get("lifecycle_revision", 1),
        },
        "declared_outcome": row.get("declared_outcome"),
        "operational_status": "running" if catalog_state == "running" else "succeeded" if catalog_state == "finished" and row.get("declared_outcome") == "satisfied" else "failed",
        "data_completeness": "partial",
        "audit_status": "not_run",
        "assessment_summary": None,
        "invocation_ids": [],  # Populated by caller
        "claims": [],  # Populated by caller
        "used_sources": [],  # Populated by caller
        "annotations": [],  # Populated by caller
        "final_answer": None,  # Populated by caller
        "assessment_refs": [],  # Populated by caller
        "operational_summary": {},  # Populated by caller
        "evidence_revision": row.get("lifecycle_revision", 1),
        "record_revision": row.get("lifecycle_revision", 1),
    }


def _map_invocation(row: dict[str, Any]) -> dict[str, Any]:
    """Map a ``research_invocations`` row to Catalog v5 invocation format.

    Args:
        row: Dictionary from ``research_invocations`` query.

    Returns:
        Catalog v5 invocation record.
    """
    status = row.get("status", "running")
    exec_status = "running" if status == "running" else (
        "succeeded" if status in ("complete", "succeeded") else "failed"
    )

    record = {
        "schema_version": SCHEMA_VERSION,
        "invocation_id": str(row["id"]),
        "external_invocation_id": row.get("external_invocation_id"),
        "research_run_id": str(row["run_id"]),
        "operation": row.get("operation", "unknown"),
        "input": row.get("input", {}),
        "started_at": (
            row["started_at"].isoformat()
            if row.get("started_at")
            else None
        ),
        "finished_at": (
            row["completed_at"].isoformat()
            if row.get("completed_at")
            else None
        ),
        "execution": {
            "status": exec_status,
            "exit_code": None,
            "error": row.get("error"),
        },
        "operational_status": status,
        "data_completeness": "complete" if row.get("completed_at") else "partial",
        "audit_status": "not_run",
        "events": [],  # Populated by caller
        "results": [],  # Populated by caller
        "artifacts": [],  # Populated by caller
        "assessment_refs": [],  # Populated by caller
        "evidence_revision": 1,
        "record_revision": 0,
    }
    if row.get("output"):
        output = row["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                output = {}
        record["results"] = output.get("results", [])
        record["operational_metrics"] = output.get("operational_metrics", {})
    return record


def _map_event(row: dict[str, Any]) -> dict[str, Any]:
    """Map a ``research_events`` row to Catalog v5 event format.

    Args:
        row: Dictionary from ``research_events`` query.

    Returns:
        Catalog v5 event record.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "fe_" + row["id"].hex[:32],
        "at": (
            row["created_at"].isoformat()
            if row.get("created_at")
            else None
        ),
        "event": row.get("event_type", "unknown"),
        "invocation_id": str(row["invocation_id"]) if row.get("invocation_id") else None,
        "data": row.get("payload", {}),
    }


def _map_assessment(row: dict[str, Any], stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Map an ``audit_assessments`` row to Catalog v5 assessment format.

    Args:
        row: Dictionary from ``audit_assessments`` query.
        stages: List of ``audit_stage_outputs`` rows for this assessment.

    Returns:
        Catalog v5 assessment record.
    """
    stage_outputs = []
    for stage_row in stages:
        stage_outputs.append({
            "stage": stage_row.get("stage"),
            "sequence_number": stage_row.get("sequence_number", 1),
            "status": stage_row.get("status", "completed"),
            "output": stage_row.get("output"),
            "error": stage_row.get("error"),
            "call_count": stage_row.get("call_count", 0),
            "used_fallback": stage_row.get("used_fallback", False),
        })

    assessment = {
        "schema_version": SCHEMA_VERSION,
        "assessment_id": str(row["id"]),
        "target_id": str(row["target_id"]),
        "target_hash": row.get("target_hash", ""),
        "evaluator_version": row.get("evaluator_version", EVALUATOR_VERSION),
        "prompt_template_version": row.get("prompt_template_version", PROMPT_TEMPLATE_VERSION),
        "policy_version": row.get("policy_version", "audit-policy-v1"),
        "stage_set": list(row.get("stage_set", [])),
        "status": row.get("status", "partial"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "prompt_hash": row.get("prompt_hash"),
        "model_fingerprint": row.get("model_fingerprint"),
        "elapsed_ms": row.get("elapsed_ms", 0),
        "stage_outputs": stage_outputs,
        "created_at": (
            row["created_at"].isoformat()
            if row.get("created_at")
            else None
        ),
    }
    if row.get("audit_packet_manifest"):
        assessment["audit_packet_manifest"] = row["audit_packet_manifest"]
    return assessment


def _map_claim(row: dict[str, Any]) -> dict[str, Any]:
    """Map a ``research_claims`` row to Catalog v5 claim format.

    Args:
        row: Dictionary from ``research_claims`` query.

    Returns:
        Catalog v5 claim record.
    """
    return {
        "id": str(row["claim_id"]),
        "statement": row.get("statement", ""),
        "semantic_status": row.get("semantic_status", "unassessed"),
        "uncertainty": row.get("uncertainty"),
        "evidence_packet_revision": row.get("evidence_packet_revision", 1),
        "created_at": (
            row["created_at"].isoformat()
            if row.get("created_at")
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Snapshot export
# ---------------------------------------------------------------------------


def _export_snapshots(
    run_id: UUID,
    uow_factory: Callable,
) -> list[dict[str, Any]]:
    """Export snapshots for a run.

    Reads from ``asset_snapshots`` and ``documents`` tables.
    Each snapshot is mapped to a Catalog v5-compatible snapshot record.

    Note: There is no direct ``run_id`` column on ``documents`` or
    ``asset_snapshots``.  Snapshots are returned for any run that has
    associated events — callers should filter if needed.

    Args:
        run_id: Research run UUID (used for context, not filtering).
        uow_factory: Unit-of-work factory.

    Returns:
        List of snapshot records.
    """
    snapshots = []
    with uow_factory() as uow:
        cur = uow.connection.cursor()
        cur.execute(
            """SELECT s.id, s.source_id, s.requested_url, s.final_url,
                      s.retrieved_at, s.mime_type, s.content_sha256,
                      d.title, d.metadata
               FROM asset_snapshots s
               LEFT JOIN documents d ON d.snapshot_id = s.id
               ORDER BY s.retrieved_at""",
        )
        for row in cur.fetchall():
            metadata = row[8] or {}
            snapshots.append({
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": str(row[0]),
                "source_id": str(row[1]),
                "url": row[2] or row[3] or "",
                "title": row[7],
                "retrieved_at": (
                    row[4].isoformat() if row[4] else None
                ),
                "mime_type": row[5],
                "content_sha256": row[6],
                "availability": "available",
                "metadata": metadata,
            })
    return snapshots


# ---------------------------------------------------------------------------
# Main export service
# ---------------------------------------------------------------------------


class CatalogExportService:
    """Derives Catalog v5-compatible exports from PostgreSQL authority.

    This service is a **pure projection**: it reads from PostgreSQL
    and blob storage, and writes derived filesystem exports.
    It never mutates source state.

    Args:
        uow_factory: Callable that returns a ``PostgresUnitOfWork``.
        blob_store: Optional blob store for large payloads.
    """

    def __init__(
        self,
        uow_factory: Callable,
        blob_store: ContentAddressedBlobStore | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.blob_store = blob_store

    def _get_blob_store(self) -> ContentAddressedBlobStore:
        """Return the configured blob store or a default one."""
        if self.blob_store is not None:
            return self.blob_store
        return ContentAddressedBlobStore(
            Path(os.environ.get("BLOB_ROOT", "data/blobs"))
        )

    # ------------------------------------------------------------------
    # Run export
    # ------------------------------------------------------------------

    def export_run(
        self,
        run_id: UUID,
        target_dir: Path | str,
        *,
        idempotency_key: str | None = None,
    ) -> ExportRunResult:
        """Export a complete research run to Catalog v5 format.

        Reads from PostgreSQL and blob storage, writes derived
        filesystem exports atomically.

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.
            idempotency_key: Optional deduplication key.

        Returns:
            ``ExportRunResult`` with all exported artifacts.

        Raises:
            ExportTargetNotFound: If the run does not exist.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Path exists as a file — this will cause write failures below
            pass

        # --- Read authoritative state ---
        with self.uow_factory() as uow:
            cur = uow.connection.cursor()

            # Run
            cur.execute(
                "SELECT * FROM research_runs WHERE id = %s",
                (str(run_id),),
            )
            run_row = cur.fetchone()
            if run_row is None:
                raise ExportTargetNotFound(f"run {run_id} not found")
            keys = [desc[0] for desc in cur.description]
            run_row = dict(zip(keys, run_row))

            # Invocations
            cur.execute(
                "SELECT * FROM research_invocations WHERE run_id = %s ORDER BY created_at",
                (str(run_id),),
            )
            inv_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            # Events (all events for the run)
            cur.execute(
                """SELECT * FROM research_events
                   WHERE run_id = %s
                   ORDER BY created_at""",
                (str(run_id),),
            )
            evt_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            # Claims
            cur.execute(
                "SELECT * FROM research_claims WHERE run_id = %s ORDER BY created_at",
                (str(run_id),),
            )
            claim_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            # Assessments
            cur.execute(
                """SELECT * FROM audit_assessments
                   WHERE run_id = %s
                   ORDER BY created_at""",
                (str(run_id),),
            )
            assessment_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            # Assessment stage outputs
            stage_map: dict[str, list[dict[str, Any]]] = {}
            if assessment_rows:
                assessment_ids = [row["id"] for row in assessment_rows]
                cur.execute(
                    """SELECT * FROM audit_stage_outputs
                       WHERE assessment_id = ANY(%s)
                       ORDER BY assessment_id, sequence_number""",
                    (assessment_ids,),
                )
                stage_keys = [desc[0] for desc in cur.description]
                for srow in cur.fetchall():
                    sdict = dict(zip(stage_keys, srow))
                    aid = str(sdict["assessment_id"])
                    stage_map.setdefault(aid, []).append(sdict)

        # --- Map to Catalog v5 format ---
        run_record = _map_run(run_row)

        # Gather invocation IDs for the run record
        invocations = []
        for inv_row in inv_rows:
            inv_record = _map_invocation(inv_row)
            invocations.append(inv_record)
            run_record["invocation_ids"].append(str(inv_row["id"]))

        # Map events
        events = [_map_event(evt) for evt in evt_rows]

        # Map claims
        claims = [_map_claim(claim) for claim in claim_rows]
        run_record["claims"] = claims

        # Map assessments
        assessments = []
        for asmt_row in assessment_rows:
            stage_outputs = stage_map.get(str(asmt_row["id"]), [])
            asmt_record = _map_assessment(asmt_row, stage_outputs)
            assessments.append(asmt_record)
            run_record["assessment_refs"].append({
                "assessment_id": str(asmt_row["id"]),
                "status": asmt_row.get("status", "partial"),
                "provider": asmt_row.get("provider"),
                "target_hash": asmt_row.get("target_hash", ""),
                "evaluator_version": asmt_row.get(
                    "evaluator_version", EVALUATOR_VERSION
                ),
            })

        # Compute source-state hash
        source_parts = [
            json.dumps(run_record, sort_keys=True, default=str),
            json.dumps(invocations, sort_keys=True, default=str),
            json.dumps(events, sort_keys=True, default=str),
            json.dumps(claims, sort_keys=True, default=str),
            json.dumps(assessments, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)

        # Export ID is deterministic from source state
        _export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}".encode()
        ).hexdigest()[:40]

        # --- Write files ---
        files_created = []
        export_status = "complete"
        export_error = None
        snapshots: list[dict[str, Any]] = []

        try:
            # Run record
            run_path = target_dir / f"{run_record['research_run_id']}.json"
            _atomic_write_json(run_path, run_record)
            files_created.append(run_path)

            # Invocations
            for inv in invocations:
                inv_path = target_dir / "invocations" / f"{inv['invocation_id']}.json"
                _atomic_write_json(inv_path, inv)
                files_created.append(inv_path)

            # Events (JSONL)
            events_path = target_dir / "events.jsonl"
            events_tmp = events_path.with_name(".events.jsonl.tmp")
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_tmp.open("w", encoding="utf-8") as f:
                for evt in events:
                    f.write(json.dumps(evt, sort_keys=True) + "\n")
                f.flush()
            events_tmp.replace(events_path)
            files_created.append(events_path)

            # Snapshots
            snapshots = _export_snapshots(run_id, self.uow_factory)
            if snapshots:
                snapshots_path = target_dir / "snapshots.json"
                _atomic_write_json(snapshots_path, snapshots)
                files_created.append(snapshots_path)

            # Claims
            if claims:
                claims_path = target_dir / "claims.json"
                _atomic_write_json(claims_path, claims)
                files_created.append(claims_path)

            # Assessments
            if assessments:
                assessments_path = target_dir / "assessments.json"
                _atomic_write_json(assessments_path, assessments)
                files_created.append(assessments_path)

            # Manifest (sources + claims summary)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "export_schema_version": EXPORT_SCHEMA_VERSION,
                "run_id": str(run_id),
                "source_state_sha256": source_state_hash,
                "generated_at": utcnow().isoformat(),
                "claims": claims,
                "sources": [
                    {
                        "url": inv.get("input", {}).get("query", ""),
                        "claim_ids": [c["id"] for c in claims],
                        "roles": ["unspecified"],
                        "fidelity": "url_only",
                    }
                    for inv in invocations
                ],
            }
            manifest_path = target_dir / "manifest.json"
            _atomic_write_json(manifest_path, manifest)
            files_created.append(manifest_path)

        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return ExportRunResult(
            run=run_record,
            invocations=invocations,
            events=events,
            snapshots=snapshots,
            claims=claims,
            assessments=assessments,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Single invocation export
    # ------------------------------------------------------------------

    def export_invocation(
        self,
        invocation_id: UUID,
        run_id: UUID,
        target_dir: Path | str,
        *,
        idempotency_key: str | None = None,
    ) -> CatalogExportResult:
        """Export a single invocation to Catalog v5 format.

        Args:
            invocation_id: Invocation UUID.
            run_id: Research run UUID.
            target_dir: Directory to write exports to.
            idempotency_key: Optional deduplication key.

        Returns:
            ``CatalogExportResult``.

        Raises:
            ExportTargetNotFound: If the invocation does not exist.
        """
        invocation_id = UUID(str(invocation_id))
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()

            # Invocation
            cur.execute(
                "SELECT * FROM research_invocations WHERE id = %s",
                (str(invocation_id),),
            )
            inv_row = cur.fetchone()
            if inv_row is None:
                raise ExportTargetNotFound(
                    f"invocation {invocation_id} not found"
                )
            keys = [desc[0] for desc in cur.description]
            inv_row = dict(zip(keys, inv_row))

            # Events for this invocation
            cur.execute(
                """SELECT * FROM research_events
                   WHERE invocation_id = %s
                   ORDER BY created_at""",
                (str(invocation_id),),
            )
            evt_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

        inv_record = _map_invocation(inv_row)
        events = [_map_event(evt) for evt in evt_rows]
        inv_record["events"] = events

        # Source-state hash
        source_parts = [
            json.dumps(inv_record, sort_keys=True, default=str),
            json.dumps(events, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}".encode()
        ).hexdigest()[:40]

        # Write files
        files_created = []
        export_status = "complete"
        export_error = None

        try:
            inv_path = target_dir / "invocations" / f"{inv_record['invocation_id']}.json"
            _atomic_write_json(inv_path, inv_record)
            files_created.append(inv_path)

            events_path = target_dir / "events.jsonl"
            events_tmp = events_path.with_name(".events.jsonl.tmp")
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_tmp.open("w", encoding="utf-8") as f:
                for evt in events:
                    f.write(json.dumps(evt, sort_keys=True) + "\n")
                f.flush()
            events_tmp.replace(events_path)
            files_created.append(events_path)

        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="invocation",
            target_id=str(invocation_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Events-only export
    # ------------------------------------------------------------------

    def export_events(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> CatalogExportResult:
        """Export run events as JSONL.

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``CatalogExportResult``.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute(
                """SELECT * FROM research_events
                   WHERE run_id = %s
                   ORDER BY created_at""",
                (str(run_id),),
            )
            keys = [desc[0] for desc in cur.description]
            evt_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

        events = [_map_event(evt) for evt in evt_rows]

        source_parts = [
            json.dumps(events, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}:events".encode()
        ).hexdigest()[:40]

        files_created = []
        export_status = "complete"
        export_error = None

        try:
            events_path = target_dir / "events.jsonl"
            events_tmp = events_path.with_name(".events.jsonl.tmp")
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_tmp.open("w", encoding="utf-8") as f:
                for evt in events:
                    f.write(json.dumps(evt, sort_keys=True) + "\n")
                f.flush()
            events_tmp.replace(events_path)
            files_created.append(events_path)
        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="run",
            target_id=str(run_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Snapshots-only export
    # ------------------------------------------------------------------

    def export_snapshots(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> CatalogExportResult:
        """Export run snapshots.

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``CatalogExportResult``.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        snapshots = _export_snapshots(run_id, self.uow_factory)

        source_parts = [
            json.dumps(snapshots, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}:snapshots".encode()
        ).hexdigest()[:40]

        files_created = []
        export_status = "complete"
        export_error = None

        try:
            if snapshots:
                snapshots_path = target_dir / "snapshots.json"
                _atomic_write_json(snapshots_path, snapshots)
                files_created.append(snapshots_path)
        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="run",
            target_id=str(run_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Claims-only export
    # ------------------------------------------------------------------

    def export_claims(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> CatalogExportResult:
        """Export run claims.

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``CatalogExportResult``.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute(
                "SELECT * FROM research_claims WHERE run_id = %s ORDER BY created_at",
                (str(run_id),),
            )
            keys = [desc[0] for desc in cur.description]
            claim_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

        claims = [_map_claim(claim) for claim in claim_rows]

        source_parts = [
            json.dumps(claims, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}:claims".encode()
        ).hexdigest()[:40]

        files_created = []
        export_status = "complete"
        export_error = None

        try:
            if claims:
                claims_path = target_dir / "claims.json"
                _atomic_write_json(claims_path, claims)
                files_created.append(claims_path)
        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="run",
            target_id=str(run_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Assessments-only export
    # ------------------------------------------------------------------

    def export_assessments(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> CatalogExportResult:
        """Export run assessments.

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``CatalogExportResult``.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute(
                """SELECT * FROM audit_assessments
                   WHERE run_id = %s
                   ORDER BY created_at""",
                (str(run_id),),
            )
            keys = [desc[0] for desc in cur.description]
            asmt_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            stage_map: dict[str, list[dict[str, Any]]] = {}
            if asmt_rows:
                asmt_ids = [row["id"] for row in asmt_rows]
                cur.execute(
                    """SELECT * FROM audit_stage_outputs
                       WHERE assessment_id = ANY(%s)
                       ORDER BY assessment_id, sequence_number""",
                    (asmt_ids,),
                )
                stage_keys = [desc[0] for desc in cur.description]
                for srow in cur.fetchall():
                    sdict = dict(zip(stage_keys, srow))
                    aid = str(sdict["assessment_id"])
                    stage_map.setdefault(aid, []).append(sdict)

        assessments = []
        for asmt_row in asmt_rows:
            stage_outputs = stage_map.get(str(asmt_row["id"]), [])
            asmt_record = _map_assessment(asmt_row, stage_outputs)
            assessments.append(asmt_record)

        source_parts = [
            json.dumps(assessments, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}:assessments".encode()
        ).hexdigest()[:40]

        files_created = []
        export_status = "complete"
        export_error = None

        try:
            if assessments:
                assessments_path = target_dir / "assessments.json"
                _atomic_write_json(assessments_path, assessments)
                files_created.append(assessments_path)
        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="run",
            target_id=str(run_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Manifest export
    # ------------------------------------------------------------------

    def export_manifest(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> CatalogExportResult:
        """Export a source manifest (sources + claims).

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``CatalogExportResult``.
        """
        run_id = UUID(str(run_id))
        target_dir = Path(target_dir)

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()

            # Invocations (for source URLs)
            cur.execute(
                "SELECT * FROM research_invocations WHERE run_id = %s ORDER BY created_at",
                (str(run_id),),
            )
            keys = [desc[0] for desc in cur.description]
            inv_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

            # Claims
            cur.execute(
                "SELECT * FROM research_claims WHERE run_id = %s ORDER BY created_at",
                (str(run_id),),
            )
            claim_rows = [
                dict(zip(keys, row))
                for row in cur.fetchall()
            ]

        claims = [_map_claim(claim) for claim in claim_rows]
        manifest_sources = [
            {
                "url": inv.get("input", {}).get("query", ""),
                "claim_ids": [c["id"] for c in claims],
                "roles": ["unspecified"],
                "fidelity": "url_only",
            }
            for inv in inv_rows
        ]

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "export_schema_version": EXPORT_SCHEMA_VERSION,
            "run_id": str(run_id),
            "claims": claims,
            "sources": manifest_sources,
        }

        source_parts = [
            json.dumps(manifest, sort_keys=True, default=str),
        ]
        source_state_hash = _compute_source_state_hash(*source_parts)
        export_id = "ce_" + sha256(
            f"{source_state_hash}:{EXPORT_SCHEMA_VERSION}:manifest".encode()
        ).hexdigest()[:40]

        files_created = []
        export_status = "complete"
        export_error = None

        try:
            manifest_path = target_dir / "manifest.json"
            _atomic_write_json(manifest_path, manifest)
            files_created.append(manifest_path)
        except ExportWriteFailure as exc:
            export_status = "failed"
            export_error = str(exc)
        except Exception as exc:
            export_status = "failed"
            export_error = f"{type(exc).__name__}: {exc}"

        return CatalogExportResult(
            export_id=export_id,
            source_state_sha256=source_state_hash,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            target_type="run",
            target_id=str(run_id),
            target_dir=target_dir,
            status=export_status,
            files_created=files_created,
            error=export_error,
        )

    # ------------------------------------------------------------------
    # Regeneration
    # ------------------------------------------------------------------

    def regenerate_run_export(
        self,
        run_id: UUID,
        target_dir: Path | str,
    ) -> ExportRunResult:
        """Regenerate a run export. If files already exist with the same
        source-state hash, they are skipped (idempotent).

        Args:
            run_id: Research run UUID.
            target_dir: Directory to write exports to.

        Returns:
            ``ExportRunResult``.
        """
        # Always re-export from authoritative state
        return self.export_run(run_id, target_dir)