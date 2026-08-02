"""Run history, retained search replay, and candidate acquisition operations."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from .direct_scrape_service import DirectScrapeRequest
from .inspection_contract import (
    _MAX_PAGE_SIZE,
    _MAX_REPLAY_BYTES,
    _SCHEMA_VERSION,
    InspectionBoundError,
    InspectionError,
    InspectionIntegrityError,
    InspectionNotFoundError,
    PageRequest,
    _decode_cursor,
    _json_value,
    _page,
    _rows,
)


def list_runs(service, page: PageRequest | None = None) -> dict[str, Any]:
    page = page or PageRequest()
    marker = _decode_cursor("runs", page.cursor)
    where = ""
    params: list[Any] = []
    if marker is not None:
        where = "WHERE (started_at,id) < (%s,%s)"
        params.extend(marker)
    params.append(page.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT id,external_run_id,objective,state,lifecycle_revision,
                       execution_mode,declared_outcome,started_at,completed_at,error
                FROM research_runs
                {where}
                ORDER BY started_at DESC,id DESC
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "external_run_id",
                "objective",
                "state",
                "lifecycle_revision",
                "execution_mode",
                "declared_outcome",
                "started_at",
                "completed_at",
                "error",
            ),
        )
    return _page("runs", rows, page.limit, timestamp_field="started_at")


def list_invocations(
    service, run: UUID | str, page: PageRequest | None = None
) -> dict[str, Any]:
    page = page or PageRequest()
    run_id, external_id = service._resolve_run(run)
    marker = _decode_cursor("invocations", page.cursor)
    where = ""
    params: list[Any] = [run_id]
    if marker is not None:
        where = "AND (created_at,id) < (%s,%s)"
        params.extend(marker)
    params.append(page.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT id,parent_invocation_id,external_invocation_id,operation,
                       status,lifecycle_revision,idempotency_key,input,output,error,
                       started_at,completed_at,created_at
                FROM research_invocations
                WHERE run_id=%s {where}
                ORDER BY created_at DESC,id DESC
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "parent_invocation_id",
                "external_invocation_id",
                "operation",
                "status",
                "lifecycle_revision",
                "idempotency_key",
                "input",
                "output",
                "error",
                "started_at",
                "completed_at",
                "created_at",
            ),
        )
    result = _page("invocations", rows, page.limit)
    result.update({"run_id": str(run_id), "external_run_id": external_id})
    return result


def list_search_responses(
    service, run: UUID | str, page: PageRequest | None = None
) -> dict[str, Any]:
    page = page or PageRequest()
    run_id, external_id = service._resolve_run(run)
    marker = _decode_cursor("search_responses", page.cursor)
    where = ""
    params: list[Any] = [run_id]
    if marker is not None:
        where = "AND (sr.created_at,sr.id) < (%s,%s)"
        params.extend(marker)
    params.append(page.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT sr.id,sr.query_text,sr.backend,sr.provider_request_id,
                       sr.status,sr.http_status,sr.parser_version,
                       sr.raw_blob_sha256,sr.raw_blob_bytes,sr.mime_type,
                       sr.content_sha256,sr.result_count,sr.error_message,
                       sr.idempotency_key,sr.requested_at,sr.responded_at,
                       sr.created_at,
                       sr.transport_metadata->>'invocation_id' AS invocation_id
                FROM search_responses sr
                WHERE sr.run_id=%s {where}
                ORDER BY sr.created_at DESC,sr.id DESC
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "query_text",
                "backend",
                "provider_request_id",
                "status",
                "http_status",
                "parser_version",
                "raw_blob_sha256",
                "raw_blob_bytes",
                "mime_type",
                "content_sha256",
                "result_count",
                "error_message",
                "idempotency_key",
                "requested_at",
                "responded_at",
                "created_at",
                "invocation_id",
            ),
        )
    result = _page("search_responses", rows, page.limit)
    result.update({"run_id": str(run_id), "external_run_id": external_id})
    return result


def replay_search(
    service, search_response_id: UUID | str, *, max_bytes: int = 1_048_576
) -> dict[str, Any]:
    response_id = UUID(str(search_response_id))
    if not 1 <= max_bytes <= _MAX_REPLAY_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {_MAX_REPLAY_BYTES}")
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT sr.id,sr.run_id,r.external_run_id,sr.query_text,sr.backend,
                      sr.provider_request_id,sr.status,sr.http_status,
                      sr.parser_version,sr.raw_blob_sha256,sr.raw_blob_bytes,
                      sr.mime_type,sr.content_sha256,sr.result_count,
                      sr.error_message,sr.transport_metadata,sr.payload_summary,
                      sr.idempotency_key,sr.requested_at,sr.responded_at,
                      sr.created_at,
                      sr.transport_metadata->>'invocation_id' AS invocation_id
               FROM search_responses sr
               JOIN research_runs r ON r.id=sr.run_id
               WHERE sr.id=%s""",
            (response_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise InspectionNotFoundError(f"search response not found: {response_id}")
        names = (
            "id",
            "run_id",
            "external_run_id",
            "query_text",
            "backend",
            "provider_request_id",
            "status",
            "http_status",
            "parser_version",
            "raw_blob_sha256",
            "raw_blob_bytes",
            "mime_type",
            "content_sha256",
            "result_count",
            "error_message",
            "transport_metadata",
            "payload_summary",
            "idempotency_key",
            "requested_at",
            "responded_at",
            "created_at",
            "invocation_id",
        )
        response = dict(zip(names, row, strict=True))
        cursor.execute(
            """SELECT o.id,o.candidate_id,o.rank,c.canonical_url,
                      c.original_url,o.title,o.snippet,c.domain,c.backend,
                      c.published_at,o.discovered_at
               FROM candidate_occurrences o
               JOIN search_candidates c
                 ON c.id=o.candidate_id AND c.run_id=o.run_id
               WHERE o.search_response_id=%s
               ORDER BY o.rank,o.id
               LIMIT %s""",
            (response_id, _MAX_PAGE_SIZE),
        )
        candidates = _rows(
            cursor,
            (
                "occurrence_id",
                "candidate_id",
                "rank",
                "canonical_url",
                "original_url",
                "title",
                "snippet",
                "domain",
                "backend",
                "published_at",
                "discovered_at",
            ),
        )
    expected_bytes = int(response["raw_blob_bytes"])
    if expected_bytes > max_bytes:
        raise InspectionBoundError(
            f"stored search payload exceeds max_bytes: {expected_bytes} > {max_bytes}"
        )
    digest = str(response["raw_blob_sha256"])
    if not service.blob_store.verify(digest):
        raise InspectionIntegrityError(
            f"retained search payload failed SHA-256 verification: {digest}"
        )
    with service.blob_store.open(digest) as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) != expected_bytes:
        raise InspectionIntegrityError(
            "retained search payload byte length does not match PostgreSQL: "
            f"{len(payload)} != {expected_bytes}"
        )
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise InspectionIntegrityError(
            f"retained search payload digest mismatch: {actual} != {digest}"
        )
    decoded: Any
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = {
            "encoding": "base64",
            "data": base64.b64encode(payload).decode(),
        }
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "search_response_replay",
        "response": _json_value(response),
        "payload": decoded,
        "payload_integrity": {
            "sha256": digest,
            "byte_length": len(payload),
            "verified": True,
        },
        "candidates": [_json_value(item) for item in candidates],
        "candidate_count": len(candidates),
        "candidates_truncated": int(response["result_count"]) > len(candidates),
    }


def scrape_candidates(
    service,
    candidate_ids: Sequence[UUID | str],
    *,
    format: str = "markdown",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not candidate_ids:
        raise ValueError("at least one candidate ID is required")
    if len(candidate_ids) > 20:
        raise InspectionBoundError("at most 20 candidates may be scraped per call")
    identifiers = tuple(UUID(str(item)) for item in candidate_ids)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,run_id FROM search_candidates
               WHERE id=ANY(%s)
               ORDER BY array_position(%s::uuid[],id)""",
            (list(identifiers), list(identifiers)),
        )
        rows = cursor.fetchall()
    found = {UUID(str(row[0])): UUID(str(row[1])) for row in rows}
    missing = [item for item in identifiers if item not in found]
    if missing:
        raise InspectionNotFoundError(
            "candidate IDs not found: " + ", ".join(str(item) for item in missing)
        )
    run_ids = set(found.values())
    if len(run_ids) != 1:
        raise InspectionError("candidate IDs must belong to one research run")
    run_id = next(iter(run_ids))
    requests = [
        DirectScrapeRequest(candidate_id=item, format=format) for item in identifiers
    ]
    # The direct service performs database/run/blob/privilege preflight before
    # constructing or invoking its Firecrawl adapter.
    result = service.direct_scrape_factory().execute(
        run_id,
        requests,
        idempotency_key=idempotency_key,
    )
    return result.to_dict()


def list_extraction_attempts(
    service,
    *,
    run: UUID | str | None = None,
    candidate_id: UUID | str | None = None,
    page: PageRequest | None = None,
) -> dict[str, Any]:
    page = page or PageRequest()
    if (run is None) == (candidate_id is None):
        raise ValueError("provide exactly one of run or candidate_id")
    params: list[Any]
    scope: dict[str, Any]
    if run is not None:
        run_id, external_id = service._resolve_run(run)
        predicate = "ea.run_id=%s"
        params = [run_id]
        scope = {"run_id": str(run_id), "external_run_id": external_id}
    else:
        candidate_uuid = UUID(str(candidate_id))
        predicate = "ea.candidate_id=%s"
        params = [candidate_uuid]
        scope = {"candidate_id": str(candidate_uuid)}
    marker = _decode_cursor("extraction_attempts", page.cursor)
    cursor_predicate = ""
    if marker is not None:
        cursor_predicate = "AND (ea.created_at,ea.id) < (%s,%s)"
        params.extend(marker)
    params.append(page.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT ea.id,ea.run_id,r.external_run_id,ea.invocation_id,
                       i.external_invocation_id,ea.candidate_id,ea.attempt_number,
                       ea.method::text,ea.method_version,ea.requested_format,
                       ea.start_time,ea.end_time,ea.exit_status::text,
                       ea.http_status,ea.backend_status,ea.raw_blob_sha256,
                       ea.raw_blob_byte_length,ea.raw_blob_mime_type,
                       ea.normalized_blob_sha256,ea.normalized_blob_byte_length,
                       ea.normalized_blob_mime_type,ea.parser_used,
                       ea.failure_class::text,ea.retry_parent_id,
                       ea.disposition::text,ea.error_message,ea.selected,
                       s.id AS snapshot_id,d.id AS document_id,
                       dd.id AS derivation_id,
                       COALESCE(array_remove(array_agg(DISTINCT c.id),NULL),'{{}}')
                         AS chunk_ids,
                       ea.created_at
                FROM extraction_attempts ea
                JOIN research_runs r ON r.id=ea.run_id
                LEFT JOIN research_invocations i ON i.id=ea.invocation_id
                LEFT JOIN asset_snapshots s ON s.extraction_attempt_id=ea.id
                LEFT JOIN documents d
                  ON d.extraction_attempt_id=ea.id OR d.snapshot_id=s.id
                LEFT JOIN document_derivations dd
                  ON dd.document_id=d.id AND dd.status='active'
                LEFT JOIN chunks c ON c.document_id=d.id
                WHERE {predicate} {cursor_predicate}
                GROUP BY ea.id,r.external_run_id,i.external_invocation_id,
                         s.id,d.id,dd.id
                ORDER BY ea.created_at DESC,ea.id DESC
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "run_id",
                "external_run_id",
                "invocation_id",
                "external_invocation_id",
                "candidate_id",
                "attempt_number",
                "method",
                "method_version",
                "requested_format",
                "start_time",
                "end_time",
                "exit_status",
                "http_status",
                "backend_status",
                "raw_blob_sha256",
                "raw_blob_byte_length",
                "raw_blob_mime_type",
                "normalized_blob_sha256",
                "normalized_blob_byte_length",
                "normalized_blob_mime_type",
                "parser_used",
                "failure_class",
                "retry_parent_id",
                "disposition",
                "error_message",
                "selected",
                "snapshot_id",
                "document_id",
                "derivation_id",
                "chunk_ids",
                "created_at",
            ),
        )
    result = _page("extraction_attempts", rows, page.limit)
    result.update(scope)
    return result
