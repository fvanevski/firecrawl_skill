"""Deterministic Catalog v5 compatibility exports from PostgreSQL authority.

The exporter captures one repeatable-read projection, verifies every referenced
blob before publication, records the attempt in ``compatibility_exports``, and
publishes a complete Catalog tree only after every staged file is durable.
Catalog files are derived output and are never read as workflow authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import ctypes
import fcntl
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from .blob import ContentAddressedBlobStore
from .invocation_events import _sanitize


EXPORT_SCHEMA_VERSION = "catalog-export-v1"
EXPORT_SCHEMA_REVISION = 1
SCHEMA_VERSION = 5
EVALUATOR_VERSION = "catalog-v5.0"
PROMPT_TEMPLATE_VERSION = "staged-research-audit-v1"
POLICY_VERSION = "audit-policy-v1"
RUN_PREFIX = "fr_"
INVOCATION_PREFIX = "fc_"
ASSESSMENT_PREFIX = "fa_"
SENSITIVE_PARAMS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "key",
    "password", "secret", "sig", "signature", "token",
}


class CatalogExportError(Exception):
    """Base exception for compatibility-export failures."""


class ExportTargetNotFound(CatalogExportError):
    """Raised when the requested authoritative target does not exist."""


class ExportWriteFailure(CatalogExportError):
    """Raised when staging, blob verification, or publication fails."""


@dataclass(frozen=True)
class CatalogExportResult:
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


@dataclass
class _Projection:
    run_row: dict[str, Any]
    invocation_rows: list[dict[str, Any]]
    event_rows: list[dict[str, Any]]
    claim_rows: list[dict[str, Any]]
    evidence_rows: list[dict[str, Any]]
    snapshot_rows: list[dict[str, Any]]
    assessment_rows: list[dict[str, Any]]
    stage_rows: list[dict[str, Any]]
    semantic_call_rows: list[dict[str, Any]]
    semantic_artifact_rows: list[dict[str, Any]]
    coverage_row: dict[str, Any] | None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()


def _catalog_id(prefix: str, value: Any, external: Any = None) -> str:
    candidate = str(external or "")
    if re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{32}}", candidate):
        return candidate
    return prefix + UUID(str(value)).hex


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise CatalogExportError("authoritative source URL is empty")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise CatalogExportError(f"invalid authoritative source URL: {raw!r}") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise CatalogExportError(f"invalid authoritative source URL: {raw!r}")
    query = [
        (key, "[REDACTED]" if key.lower() in SENSITIVE_PARAMS else val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), "")
    )


def _fetchall(cur: Any) -> list[dict[str, Any]]:
    keys = [item[0] for item in cur.description]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def _fetchone(cur: Any) -> dict[str, Any] | None:
    keys = [item[0] for item in cur.description]
    row = cur.fetchone()
    return dict(zip(keys, row)) if row is not None else None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ExportWriteFailure(f"atomic write failed for {path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(path, json.dumps(payload, indent=2, sort_keys=True, default=str).encode() + b"\n")


def _rename_exchange(left: Path, right: Path) -> bool:
    """Atomically exchange two directories on Linux; return False if unsupported."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {22, 38, 95}:
        return False
    raise OSError(error, os.strerror(error))


def _publish_directory(staging: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.catalog-export.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if target.exists() and not target.is_dir():
                raise ExportWriteFailure(f"target path exists as a file, not a directory: {target}")
            if not target.exists():
                os.replace(staging, target)
            elif _rename_exchange(staging, target):
                shutil.rmtree(staging)
            else:
                backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.old-", dir=target.parent))
                backup.rmdir()
                os.replace(target, backup)
                try:
                    os.replace(staging, target)
                except Exception:
                    os.replace(backup, target)
                    raise
                shutil.rmtree(backup)
            _fsync_directory(target.parent)
        except ExportWriteFailure:
            raise
        except OSError as exc:
            raise ExportWriteFailure(f"atomic publication failed for {target}: {exc}") from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _map_claim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["claim_id"]),
        "statement": row.get("statement", ""),
        "semantic_status": row.get("semantic_status", "unassessed"),
        "uncertainty": row.get("uncertainty"),
        "evidence_packet_revision": row.get("evidence_packet_revision", 1),
        "created_at": _iso(row.get("created_at")),
    }


def _map_event(row: dict[str, Any], run_catalog_id: str, invocation_ids: dict[str, str]) -> dict[str, Any]:
    invocation_id = row.get("invocation_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "fe_" + UUID(str(row["id"])).hex,
        "at": _iso(row.get("created_at")),
        "event": row.get("event_type", "unknown"),
        "research_run_id": run_catalog_id,
        "invocation_id": invocation_ids.get(str(invocation_id)) if invocation_id else None,
        "record_revision": row.get("run_revision"),
        "sequence_number": row.get("sequence_number"),
        "data": _sanitize(row.get("payload") or {}),
    }


def _map_semantic_call(row: dict[str, Any], artifacts: list[dict[str, Any]], invocation_ids: dict[str, str]) -> dict[str, Any]:
    invocation_id = row.get("invocation_id")
    return _sanitize({
        "call_id": str(row["id"]),
        "invocation_id": invocation_ids.get(str(invocation_id)) if invocation_id else None,
        "stage": row.get("stage"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "model_revision": row.get("model_revision", ""),
        "prompt_version": row.get("prompt_version"),
        "input_sha256": row.get("input_sha256"),
        "request": row.get("request") or {},
        "response_metadata": row.get("response_metadata") or {},
        "status": row.get("status"),
        "error": row.get("error"),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "artifacts": artifacts,
    })


class CatalogExportService:
    """Project committed PostgreSQL/blob state into a Catalog v5 tree."""

    def __init__(self, uow_factory: Callable, blob_store: ContentAddressedBlobStore | None = None) -> None:
        self.uow_factory = uow_factory
        self.blob_store = blob_store

    def _load_projection(self, run_id: UUID) -> _Projection:
        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT * FROM research_runs WHERE id=%s", (str(run_id),))
            run = _fetchone(cur)
            if run is None:
                raise ExportTargetNotFound(f"run {run_id} not found")

            cur.execute("SELECT * FROM research_invocations WHERE run_id=%s ORDER BY created_at,id", (str(run_id),))
            invocations = _fetchall(cur)
            cur.execute("SELECT * FROM research_events WHERE run_id=%s ORDER BY sequence_number,id", (str(run_id),))
            events = _fetchall(cur)
            cur.execute("SELECT * FROM research_claims WHERE run_id=%s ORDER BY created_at,id", (str(run_id),))
            claims = _fetchall(cur)
            cur.execute(
                """SELECT cel.*, c.text AS passage_text,
                          c.content_sha256 AS passage_sha256,
                          s.canonical_url, sc.id AS candidate_id
                   FROM claim_evidence_links cel
                   JOIN chunks c ON c.id=cel.passage_id
                   JOIN documents d ON d.id=c.document_id
                   JOIN asset_snapshots snap ON snap.id=cel.snapshot_id
                   JOIN sources s ON s.id=snap.source_id
                   LEFT JOIN search_candidates sc
                     ON sc.run_id=cel.run_id AND sc.canonical_url=s.canonical_url
                   WHERE cel.run_id=%s
                   ORDER BY cel.claim_id,cel.created_at,cel.id""",
                (str(run_id),),
            )
            evidence = _fetchall(cur)
            cur.execute(
                """WITH run_snapshot_ids AS (
                       SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                       UNION
                       SELECT snapshot_id FROM claim_evidence_links WHERE run_id=%s
                   )
                   SELECT snap.id,snap.source_id,snap.requested_url,snap.final_url,
                          snap.retrieved_at,snap.mime_type,snap.content_sha256,
                          snap.raw_blob_uri,snap.raw_byte_length,snap.firecrawl_version,
                          snap.crawl_options,src.canonical_url,d.title,d.author,
                          d.published_at,d.language,d.parser_name,d.parser_version,
                          d.normalization_version,d.document_sha256,d.metadata
                   FROM run_snapshot_ids rs
                   JOIN asset_snapshots snap ON snap.id=rs.snapshot_id
                   JOIN sources src ON src.id=snap.source_id
                   LEFT JOIN documents d ON d.snapshot_id=snap.id
                   ORDER BY snap.retrieved_at,snap.id""",
                (str(run_id), str(run_id)),
            )
            snapshots = _fetchall(cur)
            cur.execute("SELECT * FROM audit_assessments WHERE run_id=%s ORDER BY created_at,id", (str(run_id),))
            assessments = _fetchall(cur)
            if assessments:
                cur.execute(
                    "SELECT * FROM audit_stage_outputs WHERE assessment_id=ANY(%s) ORDER BY assessment_id,stage,sequence_number,id",
                    ([row["id"] for row in assessments],),
                )
                stages = _fetchall(cur)
            else:
                stages = []
            cur.execute("SELECT * FROM semantic_calls WHERE run_id=%s ORDER BY created_at,id", (str(run_id),))
            calls = _fetchall(cur)
            if calls:
                cur.execute(
                    "SELECT * FROM semantic_artifacts WHERE semantic_call_id=ANY(%s) ORDER BY semantic_call_id,created_at,id",
                    ([row["id"] for row in calls],),
                )
                artifacts = _fetchall(cur)
            else:
                artifacts = []
            cur.execute(
                "SELECT * FROM coverage_snapshots WHERE run_id=%s ORDER BY coverage_revision DESC LIMIT 1",
                (str(run_id),),
            )
            coverage = _fetchone(cur)
        return _Projection(run, invocations, events, claims, evidence, snapshots, assessments, stages, calls, artifacts, coverage)

    def _snapshot_records(self, projection: _Projection) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for row in projection.snapshot_rows:
            digest = str(row.get("content_sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ExportWriteFailure(f"snapshot {row['id']} has invalid content SHA-256")
            canonical = _canonical_url(row.get("canonical_url") or row.get("final_url") or row.get("requested_url"))
            records.append({
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": str(row["id"]),
                "source_id": str(row["source_id"]),
                "url": canonical,
                "requested_url": _canonical_url(row.get("requested_url") or canonical),
                "title": row.get("title"),
                "author": row.get("author"),
                "published_at": _iso(row.get("published_at")),
                "retrieved_at": _iso(row.get("retrieved_at")),
                "mime_type": row.get("mime_type"),
                "content_sha256": digest,
                "raw_blob_uri": row.get("raw_blob_uri"),
                "raw_byte_length": row.get("raw_byte_length"),
                "firecrawl_version": row.get("firecrawl_version"),
                "parser": {
                    "name": row.get("parser_name"),
                    "version": row.get("parser_version"),
                    "normalization_version": row.get("normalization_version"),
                },
                "document_sha256": row.get("document_sha256"),
                "blob_path": f"blobs/sha256/{digest[:2]}/{digest}",
                "availability": "available",
                "metadata": _sanitize(row.get("metadata") or {}),
            })
        return records

    @staticmethod
    def _manifest(projection: _Projection, claims: list[dict[str, Any]]) -> dict[str, Any]:
        known_claims = {claim["id"] for claim in claims}
        sources: dict[tuple[str, str], dict[str, Any]] = {}
        for link in projection.evidence_rows:
            claim_id = str(link["claim_id"])
            if claim_id not in known_claims:
                raise CatalogExportError(f"evidence link references unknown claim {claim_id}")
            url = _canonical_url(link.get("canonical_url") or link.get("source_url"))
            candidate_id = str(link["candidate_id"]) if link.get("candidate_id") else ""
            key = (url, candidate_id)
            source = sources.setdefault(key, {
                "url": url,
                "canonical_url": url,
                "candidate_id": candidate_id or None,
                "snapshot_id": str(link["snapshot_id"]),
                "claim_ids": [],
                "passage_ids": [],
                "roles": [],
                "relationships": [],
                "fidelity": "authoritative_passage",
                "resolution": "matched",
            })
            source["claim_ids"].append(claim_id)
            source["passage_ids"].append(str(link["passage_id"]))
            relationship = str(link.get("relationship") or "supports")
            source["roles"].append(relationship)
            source["relationships"].append({
                "claim_id": claim_id,
                "passage_id": str(link["passage_id"]),
                "snapshot_id": str(link["snapshot_id"]),
                "relationship": relationship,
                "confidence": link.get("confidence"),
                "passage_sha256": link.get("passage_sha256"),
            })
        output = []
        for source in sources.values():
            for key in ("claim_ids", "passage_ids", "roles"):
                source[key] = sorted(set(source[key]))
            source["relationships"].sort(key=lambda item: (item["claim_id"], item["passage_id"]))
            output.append(source)
        output.sort(key=lambda item: (item["canonical_url"], item.get("candidate_id") or ""))
        return {"schema_version": SCHEMA_VERSION, "claims": claims, "sources": output}

    def _map_records(self, projection: _Projection) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        run_row = projection.run_row
        run_catalog_id = _catalog_id(RUN_PREFIX, run_row["id"], run_row.get("external_run_id") or run_row.get("external_id"))
        invocation_ids = {
            str(row["id"]): _catalog_id(INVOCATION_PREFIX, row["id"], row.get("external_invocation_id"))
            for row in projection.invocation_rows
        }
        claims = [_map_claim(row) for row in projection.claim_rows]
        snapshots = self._snapshot_records(projection)
        manifest = self._manifest(projection, claims)

        artifacts_by_call: dict[str, list[dict[str, Any]]] = {}
        for artifact in projection.semantic_artifact_rows:
            artifacts_by_call.setdefault(str(artifact["semantic_call_id"]), []).append(_sanitize({
                "artifact_id": str(artifact["id"]),
                "artifact_type": artifact.get("artifact_type"),
                "schema_name": artifact.get("schema_name"),
                "schema_version": artifact.get("schema_version"),
                "content_sha256": artifact.get("content_sha256"),
                "validation_status": artifact.get("validation_status"),
                "validation_errors": artifact.get("validation_errors") or [],
            }))
        mapped_calls = {
            str(row["id"]): _map_semantic_call(
                row, artifacts_by_call.get(str(row["id"]), []), invocation_ids
            )
            for row in projection.semantic_call_rows
        }

        stages_by_assessment: dict[str, list[dict[str, Any]]] = {}
        for row in projection.stage_rows:
            stages_by_assessment.setdefault(str(row["assessment_id"]), []).append(row)
        assessments: list[dict[str, Any]] = []
        assessment_id_map: dict[str, str] = {}
        for row in projection.assessment_rows:
            aid = ASSESSMENT_PREFIX + UUID(str(row["id"])).hex
            assessment_id_map[str(row["id"])] = aid
            target = run_catalog_id if row.get("target_type") == "run" else invocation_ids.get(str(row.get("target_id")))
            if target is None:
                raise CatalogExportError(f"assessment {row['id']} references unknown target {row.get('target_id')}")
            stage_values: dict[str, Any] = {}
            stage_errors: list[dict[str, Any]] = []
            call_ids = set((row.get("audit_packet_manifest") or {}).get("semantic_call_ids", []))
            for stage in stages_by_assessment.get(str(row["id"]), []):
                name = str(stage["stage"])
                stage_output = stage.get("output") or {}
                if isinstance(stage_output, dict):
                    if stage_output.get("semantic_call_id"):
                        call_ids.add(stage_output["semantic_call_id"])
                    call_ids.update(stage_output.get("semantic_call_ids") or [])
                if stage.get("status") == "failed":
                    stage_errors.append({
                        "stage": name,
                        "sequence_number": stage.get("sequence_number"),
                        "error": stage.get("error"),
                        "error_details": stage.get("error_details") or {},
                    })
                    continue
                value = stage.get("output")
                if name in stage_values:
                    if not isinstance(stage_values[name], list):
                        stage_values[name] = [stage_values[name]]
                    stage_values[name].append(value)
                else:
                    stage_values[name] = value
            unknown_calls = sorted(str(item) for item in call_ids if str(item) not in mapped_calls)
            if unknown_calls:
                raise CatalogExportError(
                    f"assessment {row['id']} references unknown semantic calls: {unknown_calls}"
                )
            assessments.append(_sanitize({
                "schema_version": SCHEMA_VERSION,
                "assessment_id": aid,
                "target_id": target,
                "target_type": row.get("target_type"),
                "target_hash": row.get("target_hash"),
                "evaluator_version": row.get("evaluator_version"),
                "prompt_template_version": row.get("prompt_template_version"),
                "policy_version": row.get("policy_version"),
                "implementation_version": EXPORT_SCHEMA_VERSION,
                "stage_set": list(row.get("stage_set") or []),
                "status": row.get("status"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "model_fingerprint": row.get("model_fingerprint"),
                "prompt_hash": row.get("prompt_hash"),
                "audit_identity_hash": row.get("audit_identity_hash"),
                "audit_packet_manifest": row.get("audit_packet_manifest") or {},
                "stages": stage_values,
                "stage_errors": stage_errors,
                "calls": [mapped_calls[str(call_id)] for call_id in sorted(call_ids, key=str)],
                "elapsed_ms": row.get("elapsed_ms", 0),
                "created_at": _iso(row.get("created_at")),
            }))

        invocations: list[dict[str, Any]] = []
        for row in projection.invocation_rows:
            status = row.get("status", "pending")
            execution = "running" if status in {"pending", "running"} else "succeeded" if status == "complete" else "failed"
            output = row.get("output") or {}
            if isinstance(output, str):
                output = json.loads(output)
            invocation_assessments = [
                item for item in assessments if item["target_type"] == "invocation" and item["target_id"] == invocation_ids[str(row["id"])]
            ]
            invocations.append(_sanitize({
                "schema_version": SCHEMA_VERSION,
                "invocation_id": invocation_ids[str(row["id"])],
                "research_run_id": run_catalog_id,
                "operation": row.get("operation", "unknown"),
                "input": row.get("input") or {},
                "started_at": _iso(row.get("started_at") or row.get("created_at")),
                "finished_at": _iso(row.get("completed_at")),
                "execution": {"status": execution, "exit_code": None, "error": row.get("error")},
                "operational_status": execution,
                "data_completeness": "complete" if status == "complete" else "partial",
                "audit_status": invocation_assessments[-1]["status"] if invocation_assessments else "not_run",
                "events": [],
                "results": output.get("results", []),
                "operational_metrics": output.get("operational_metrics", {}),
                "artifacts": [],
                "assessment_refs": [{
                    "assessment_id": item["assessment_id"], "status": item["status"],
                    "provider": item.get("provider"), "target_hash": item["target_hash"],
                    "evaluator_version": item["evaluator_version"],
                } for item in invocation_assessments],
                "evidence_revision": row.get("lifecycle_revision", 1),
                "record_revision": row.get("lifecycle_revision", 1),
            }))

        events = [_map_event(row, run_catalog_id, invocation_ids) for row in projection.event_rows]
        events_by_invocation: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("invocation_id"):
                events_by_invocation.setdefault(event["invocation_id"], []).append({
                    "type": event["event"], "at": event["at"], **event["data"]
                })
        for invocation in invocations:
            invocation["events"] = events_by_invocation.get(invocation["invocation_id"], [])
            if snapshots:
                _, compressed = self._invocation_snapshot_archive(invocation, snapshots)
                relative = Path("snapshots") / f"{invocation['invocation_id']}.json.gz"
                invocation["snapshot"] = {
                    "path": str(relative),
                    "type": "catalog_snapshot",
                    "size_bytes": len(compressed),
                    "sha256": sha256(compressed).hexdigest(),
                    "availability": "available",
                    "truncated": False,
                    "original_result_count": len(invocation.get("results", [])),
                    "retained_result_count": len(invocation.get("results", [])),
                }

        run_assessments = [item for item in assessments if item["target_type"] == "run"]
        state = run_row.get("state", "created")
        terminal = state in {"completed", "partial", "failed", "cancelled"}
        outcome = run_row.get("declared_outcome")
        operational_status = "running" if not terminal else "succeeded" if state == "completed" else "failed" if state in {"failed", "cancelled"} else "partial"
        coverage = projection.coverage_row or {}
        run = _sanitize({
            "schema_version": SCHEMA_VERSION,
            "research_run_id": run_catalog_id,
            "objective": run_row.get("objective", ""),
            "profile": {"requested": run_row.get("execution_mode"), "selected": run_row.get("execution_mode"), "reason": "authoritative execution mode"},
            "started_at": _iso(run_row.get("started_at")),
            "updated_at": _iso(run_row.get("updated_at") or run_row.get("started_at")),
            "finished_at": _iso(run_row.get("finished_at") or run_row.get("completed_at")),
            "lifecycle": {"state": "finished" if terminal else "running", "revision": run_row.get("lifecycle_revision", 1)},
            "declared_outcome": outcome,
            "operational_status": operational_status,
            "data_completeness": "complete" if state == "completed" and claims and manifest["sources"] else "partial",
            "audit_status": run_assessments[-1]["status"] if run_assessments else "not_run",
            "assessment_summary": run_assessments[-1]["stages"].get("synthesis") if run_assessments else None,
            "invocation_ids": [item["invocation_id"] for item in invocations],
            "claims": claims,
            "used_sources": manifest["sources"],
            "annotations": [event for event in events if event["event"] == "annotation"],
            "final_answer": None,
            "assessment_refs": [{
                "assessment_id": item["assessment_id"], "status": item["status"],
                "provider": item.get("provider"), "target_hash": item["target_hash"],
                "evaluator_version": item["evaluator_version"],
            } for item in run_assessments],
            "operational_summary": {
                "operations": len(invocations),
                "succeeded": sum(item["execution"]["status"] == "succeeded" for item in invocations),
                "failed": sum(item["execution"]["status"] == "failed" for item in invocations),
                "snapshots": len(snapshots),
                "claims": len(claims),
                "coverage_revision": coverage.get("coverage_revision", run_row.get("current_coverage_revision", 0)),
                "coverage": coverage.get("ledger", {}),
            },
            "evidence_revision": run_row.get("current_coverage_revision", run_row.get("lifecycle_revision", 1)),
            "record_revision": run_row.get("lifecycle_revision", 1),
        })
        return run, invocations, events, snapshots, claims, assessments, manifest

    @staticmethod
    def _source_hash(records: Iterable[Any]) -> str:
        return sha256(b"\x00".join(_canonical_bytes(item) for item in records)).hexdigest()

    def _record_export(self, run_id: UUID, projection: _Projection, target_dir: Path, export_type: str, source_hash: str, status: str, attempt_key: str, error: str | None = None) -> None:
        event_cursor = projection.event_rows[-1]["id"] if projection.event_rows else None
        with self.uow_factory() as uow:
            uow.runs.record_compatibility_export(
                run_id, export_type, EXPORT_SCHEMA_REVISION, source_hash, status, attempt_key,
                database_revision=projection.run_row.get("lifecycle_revision", 0),
                event_cursor=event_cursor,
                filesystem_path=str(target_dir.resolve()),
                error=error,
                metadata={"export_schema": EXPORT_SCHEMA_VERSION},
            )

    def _stage_tree(self, target_dir: Path, writer: Callable[[Path], None]) -> list[Path]:
        target_dir = target_dir.resolve()
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.stage-", dir=target_dir.parent))
        try:
            writer(staging)
            for directory in sorted((item for item in staging.rglob("*") if item.is_dir()), reverse=True):
                _fsync_directory(directory)
            _fsync_directory(staging)
            relative_files = [item.relative_to(staging) for item in sorted(staging.rglob("*")) if item.is_file()]
            _publish_directory(staging, target_dir)
            return [target_dir / item for item in relative_files]
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _write_snapshots(self, root: Path, snapshots: list[dict[str, Any]], invocations: list[dict[str, Any]]) -> None:
        if not snapshots:
            return
        _atomic_write_json(root / "snapshots" / "index.json", snapshots)
        for snapshot in snapshots:
            digest = snapshot["content_sha256"]
            if self.blob_store is None or not self.blob_store.verify(digest):
                raise ExportWriteFailure(
                    "authoritative blob missing or corrupt for snapshot "
                    f"{snapshot['snapshot_id']}: {digest}"
                )
            with self.blob_store.open(digest) as handle:
                _atomic_write_bytes(root / snapshot["blob_path"], handle.read())
        for invocation in invocations:
            _, compressed = self._invocation_snapshot_archive(invocation, snapshots)
            relative = Path("snapshots") / f"{invocation['invocation_id']}.json.gz"
            _atomic_write_bytes(root / relative, compressed)

    @staticmethod
    def _invocation_snapshot_archive(invocation: dict[str, Any], snapshots: list[dict[str, Any]]) -> tuple[dict[str, Any], bytes]:
        by_url = {item["url"]: item for item in snapshots}
        urls = set()
        for result in invocation.get("results", []):
            try:
                urls.add(_canonical_url(result.get("canonical_url") or result.get("url")))
            except CatalogExportError:
                continue
        linked = [by_url[url] for url in sorted(urls) if url in by_url]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "invocation_id": invocation["invocation_id"],
            "created_at": invocation.get("finished_at") or invocation.get("started_at"),
            "topic": invocation.get("input", {}).get("topic") or invocation.get("input", {}).get("query") or "",
            "results": invocation.get("results", []),
            "authoritative_snapshots": linked,
            "truncation": {
                "truncated": False,
                "original_result_count": len(invocation.get("results", [])),
                "retained_result_count": len(invocation.get("results", [])),
            },
        }
        return payload, gzip.compress(_canonical_bytes(payload), mtime=0)

    def export_run(self, run_id: UUID, target_dir: Path | str) -> ExportRunResult:
        run_id = UUID(str(run_id))
        target = Path(target_dir)
        projection = self._load_projection(run_id)
        run, invocations, events, snapshots, claims, assessments, manifest = self._map_records(projection)
        source_hash = self._source_hash((run, invocations, events, snapshots, claims, assessments, manifest))
        manifest = {**manifest, "export_schema_version": EXPORT_SCHEMA_VERSION, "run_id": run["research_run_id"], "source_state_sha256": source_hash}
        attempt_key = f"catalog-v5:run-attempt:{uuid4()}"
        self._record_export(run_id, projection, target, "catalog_v5_run", source_hash, "pending", attempt_key)
        files: list[Path] = []
        error = None
        try:
            def write(root: Path) -> None:
                self._write_snapshots(root, snapshots, invocations)
                _atomic_write_json(root / "runs" / f"{run['research_run_id']}.json", run)
                for invocation in invocations:
                    _atomic_write_json(root / "invocations" / f"{invocation['invocation_id']}.json", invocation)
                _atomic_write_bytes(root / "events.jsonl", b"".join(_canonical_bytes(event) for event in events))
                for assessment in assessments:
                    _atomic_write_json(root / "assessments" / assessment["target_id"] / f"{assessment['assessment_id']}.json", assessment)
                _atomic_write_json(root / "manifest.json", manifest)
            files = self._stage_tree(target, write)
            self._record_export(run_id, projection, target, "catalog_v5_run", source_hash, "complete", attempt_key)
            status = "complete"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._record_export(run_id, projection, target, "catalog_v5_run", source_hash, "failed", attempt_key, error)
            except Exception as record_exc:
                error += f"; failure recording failed: {type(record_exc).__name__}: {record_exc}"
            status = "failed"
        return ExportRunResult(run, invocations, events, snapshots, claims, assessments, source_hash, EXPORT_SCHEMA_VERSION, target, status, files, error)

    def _export_subset(self, run_id: UUID, target_dir: Path | str, export_type: str, writer_factory: Callable[[_Projection, tuple[Any, ...]], Callable[[Path], None]]) -> CatalogExportResult:
        run_id = UUID(str(run_id))
        target = Path(target_dir)
        projection = self._load_projection(run_id)
        records = self._map_records(projection)
        source_hash = self._source_hash(records)
        export_id = "ce_" + sha256(f"{source_hash}:{EXPORT_SCHEMA_VERSION}:{export_type}".encode()).hexdigest()[:40]
        attempt_key = f"catalog-v5:{export_type}-attempt:{uuid4()}"
        self._record_export(run_id, projection, target, export_type, source_hash, "pending", attempt_key)
        try:
            files = self._stage_tree(target, writer_factory(projection, records))
            self._record_export(run_id, projection, target, export_type, source_hash, "complete", attempt_key)
            return CatalogExportResult(export_id, source_hash, EXPORT_SCHEMA_VERSION, "run", str(run_id), target, "complete", files)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._record_export(run_id, projection, target, export_type, source_hash, "failed", attempt_key, error)
            except Exception as record_exc:
                error += f"; failure recording failed: {type(record_exc).__name__}: {record_exc}"
            return CatalogExportResult(export_id, source_hash, EXPORT_SCHEMA_VERSION, "run", str(run_id), target, "failed", [], error)

    def export_invocation(self, invocation_id: UUID, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        invocation_id = UUID(str(invocation_id))
        projection = self._load_projection(UUID(str(run_id)))
        records = self._map_records(projection)
        invocation = next((item for row, item in zip(projection.invocation_rows, records[1]) if UUID(str(row["id"])) == invocation_id), None)
        if invocation is None:
            raise ExportTargetNotFound(f"invocation {invocation_id} not found for run {run_id}")
        def factory(_projection: _Projection, _records: tuple[Any, ...]) -> Callable[[Path], None]:
            return lambda root: _atomic_write_json(root / "invocations" / f"{invocation['invocation_id']}.json", invocation)
        result = self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_invocation", factory)
        return CatalogExportResult(result.export_id, result.source_state_sha256, result.export_schema_version, "invocation", str(invocation_id), result.target_dir, result.status, result.files_created, result.error)

    def export_events(self, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        return self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_events", lambda _p, r: lambda root: _atomic_write_bytes(root / "events.jsonl", b"".join(_canonical_bytes(item) for item in r[2])))

    def export_snapshots(self, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        return self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_snapshots", lambda _p, r: lambda root: self._write_snapshots(root, r[3], r[1]))

    def export_claims(self, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        return self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_claims", lambda _p, r: lambda root: _atomic_write_json(root / "claims.json", r[4]))

    def export_assessments(self, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        def factory(_projection: _Projection, records: tuple[Any, ...]) -> Callable[[Path], None]:
            def write(root: Path) -> None:
                for item in records[5]:
                    _atomic_write_json(root / "assessments" / item["target_id"] / f"{item['assessment_id']}.json", item)
            return write
        return self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_assessments", factory)

    def export_manifest(self, run_id: UUID, target_dir: Path | str) -> CatalogExportResult:
        def factory(_projection: _Projection, records: tuple[Any, ...]) -> Callable[[Path], None]:
            source_hash = self._source_hash(records)
            payload = {**records[6], "export_schema_version": EXPORT_SCHEMA_VERSION, "run_id": records[0]["research_run_id"], "source_state_sha256": source_hash}
            return lambda root: _atomic_write_json(root / "manifest.json", payload)
        return self._export_subset(UUID(str(run_id)), target_dir, "catalog_v5_manifest", factory)

    def regenerate_run_export(self, run_id: UUID, target_dir: Path | str) -> ExportRunResult:
        return self.export_run(run_id, target_dir)
