"""Run history, retained search replay, and candidate acquisition operations."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from .direct_scrape_service import DirectScrapeRequest
from .inspection_contract import (
    _MAX_HISTORY_OUTPUT_CHARS,
    _MAX_PAGE_SIZE,
    _MAX_REPLAY_BYTES,
    _SCHEMA_VERSION,
    InspectionBoundError,
    InspectionError,
    InspectionIntegrityError,
    InspectionNotFoundError,
    PageRequest,
    _bound_scrape_result,
    _bounded_identities,
    _bounded_json,
    _bounded_text,
    _decode_cursor,
    _finalize_payload,
    _json_value,
    _page,
    _rows,
    _scope_fingerprint,
)


def list_runs(service, page: PageRequest | None = None) -> dict[str, Any]:
    page = page or PageRequest()
    scope = _scope_fingerprint("runs")
    marker = _decode_cursor("runs", page.cursor, scope=scope)
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
    for row in rows:
        row["objective"] = _bounded_text(row.get("objective"))
        row["error"] = _bounded_text(row.get("error"))
    return _finalize_payload(
        _page("runs", rows, page.limit, scope=scope, timestamp_field="started_at"),
        max_chars=_MAX_HISTORY_OUTPUT_CHARS,
    )


def list_invocations(
    service, run: UUID | str, page: PageRequest | None = None
) -> dict[str, Any]:
    page = page or PageRequest()
    run_id, external_id = service._resolve_run(run)
    scope = _scope_fingerprint("invocations", run_id=run_id)
    marker = _decode_cursor("invocations", page.cursor, scope=scope)
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
    for row in rows:
        row["input"] = _bounded_json(row.get("input"))
        row["output"] = _bounded_json(row.get("output"))
        row["error"] = _bounded_text(row.get("error"))
    result = _page("invocations", rows, page.limit, scope=scope)
    result.update({"run_id": str(run_id), "external_run_id": external_id})
    return _finalize_payload(result, max_chars=_MAX_HISTORY_OUTPUT_CHARS)


def list_search_responses(
    service, run: UUID | str, page: PageRequest | None = None
) -> dict[str, Any]:
    page = page or PageRequest()
    run_id, external_id = service._resolve_run(run)
    scope = _scope_fingerprint("search_responses", run_id=run_id)
    marker = _decode_cursor("search_responses", page.cursor, scope=scope)
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
    for row in rows:
        row["query_text"] = _bounded_text(row.get("query_text"))
        row["error_message"] = _bounded_text(row.get("error_message"))
    result = _page("search_responses", rows, page.limit, scope=scope)
    result.update({"run_id": str(run_id), "external_run_id": external_id})
    return _finalize_payload(result, max_chars=_MAX_HISTORY_OUTPUT_CHARS)


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
            (response_id, _MAX_PAGE_SIZE + 1),
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
    try:
        decoded: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = {"encoding": "base64", "data": base64.b64encode(payload).decode()}
    selected_candidates = candidates[:_MAX_PAGE_SIZE]
    for candidate in selected_candidates:
        for name, field_value in list(candidate.items()):
            if isinstance(field_value, str):
                candidate[name] = _bounded_text(field_value)
    response["query_text"] = _bounded_text(response.get("query_text"))
    response["error_message"] = _bounded_text(response.get("error_message"))
    response["transport_metadata"] = _bounded_json(response.get("transport_metadata"))
    response["payload_summary"] = _bounded_json(response.get("payload_summary"))
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "search_response_replay",
            "response": _json_value(response),
            "payload": decoded,
            "payload_integrity": {
                "sha256": digest,
                "byte_length": len(payload),
                "verified": True,
            },
            "candidates": [_json_value(item) for item in selected_candidates],
            "candidate_count": len(candidates),
            "candidates_truncated": len(candidates) > len(selected_candidates)
            or int(response["result_count"]) > len(selected_candidates),
        },
        max_chars=max(_MAX_HISTORY_OUTPUT_CHARS, max_bytes * 2 + 131_072),
    )


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
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("candidate IDs must be unique within one acquisition batch")
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
    result = service.direct_scrape_factory().execute(
        run_id,
        requests,
        idempotency_key=idempotency_key,
    )
    return _bound_scrape_result(result.to_dict(), kind="candidate_scrape")


def retry_candidates(
    service,
    prior_invocation_id: UUID | str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("retry idempotency_key is required")
    prior_id = UUID(str(prior_invocation_id))
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT run_id,input FROM research_invocations
               WHERE id=%s AND operation='direct_scrape'""",
            (prior_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise InspectionNotFoundError(
            f"prior direct-scrape invocation not found: {prior_id}"
        )
    run_id = UUID(str(row[0]))
    stored_input = row[1] or {}
    if isinstance(stored_input, str):
        stored_input = json.loads(stored_input)
    requests: list[DirectScrapeRequest] = []
    for raw in stored_input.get("requests") or []:
        if raw.get("url") is not None or not raw.get("candidate_id"):
            raise InspectionError(
                "retry-candidates accepts only prior candidate-ID acquisitions"
            )
        requests.append(
            DirectScrapeRequest(
                candidate_id=UUID(str(raw["candidate_id"])),
                format=str(raw.get("format") or "markdown"),
                summary=bool(raw.get("summary")),
                schema=raw.get("schema"),
                mime_type=raw.get("mime_type"),
                options=raw.get("options") or {},
            )
        )
    if not requests:
        raise InspectionError("prior invocation contains no candidate requests")
    result = service.direct_scrape_factory().retry_failed(
        run_id,
        requests,
        prior_invocation_id=prior_id,
        idempotency_key=idempotency_key,
    )
    return _bound_scrape_result(result.to_dict(), kind="candidate_retry")


def _invocation_item_map(
    connection: Any,
    invocation_ids: Sequence[UUID],
) -> dict[UUID, dict[str, Any]]:
    if not invocation_ids:
        return {}
    values: dict[UUID, dict[str, Any]] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,output FROM research_invocations WHERE id=ANY(%s)",
            (list(dict.fromkeys(invocation_ids)),),
        )
        for _invocation_id, output in cursor.fetchall():
            if isinstance(output, str):
                output = json.loads(output)
            for item in (output or {}).get("items") or []:
                attempt_id = item.get("extraction_attempt_id")
                if attempt_id:
                    values[UUID(str(attempt_id))] = dict(item)
    return values


def _apply_attempt_result(row: dict[str, Any], item: Mapping[str, Any] | None) -> None:
    if item is not None:
        for name in ("source_id", "snapshot_id", "document_id", "derivation_id"):
            if item.get(name) is not None:
                row[name] = item[name]
        chunk_ids = list(item.get("chunk_ids") or [])
        row["chunk_ids"] = _bounded_identities(chunk_ids)
    else:
        fallback = list(row.pop("fallback_chunk_ids") or [])
        total = int(row.pop("fallback_chunk_count") or 0)
        row["chunk_ids"] = _bounded_identities(fallback, total=total)
    row.pop("fallback_chunk_ids", None)
    row.pop("fallback_chunk_count", None)
    for name, field_value in list(row.items()):
        if isinstance(field_value, str):
            row[name] = _bounded_text(field_value)
        elif isinstance(field_value, Mapping):
            row[name] = _bounded_json(field_value)


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
    scope_values: dict[str, Any]
    scope_result: dict[str, Any]
    if run is not None:
        run_id, external_id = service._resolve_run(run)
        predicate = "ea.run_id=%s"
        params = [run_id]
        scope_values = {"run_id": run_id}
        scope_result = {"run_id": str(run_id), "external_run_id": external_id}
    else:
        candidate_uuid = UUID(str(candidate_id))
        with service.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT c.run_id,r.external_run_id FROM search_candidates c
                   JOIN research_runs r ON r.id=c.run_id WHERE c.id=%s""",
                (candidate_uuid,),
            )
            candidate_row = cursor.fetchone()
        if candidate_row is None:
            raise InspectionNotFoundError(f"candidate not found: {candidate_uuid}")
        predicate = "ea.candidate_id=%s"
        params = [candidate_uuid]
        scope_values = {"candidate_id": candidate_uuid}
        scope_result = {
            "candidate_id": str(candidate_uuid),
            "run_id": str(candidate_row[0]),
            "external_run_id": candidate_row[1],
        }
    scope = _scope_fingerprint("extraction_attempts", **scope_values)
    marker = _decode_cursor("extraction_attempts", page.cursor, scope=scope)
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
                       (SELECT s0.source_id FROM asset_snapshots s0
                          WHERE s0.extraction_attempt_id=ea.id
                          ORDER BY s0.retrieved_at DESC,s0.id DESC
                          LIMIT 1) AS source_id,
                       (SELECT s.id FROM asset_snapshots s
                          WHERE s.extraction_attempt_id=ea.id
                          ORDER BY s.retrieved_at DESC,s.id DESC
                          LIMIT 1) AS snapshot_id,
                       (SELECT d.id FROM documents d
                          WHERE d.extraction_attempt_id=ea.id
                          ORDER BY d.id DESC LIMIT 1) AS document_id,
                       (SELECT dd.id FROM document_derivations dd
                          WHERE dd.document_id=(SELECT d2.id FROM documents d2
                             WHERE d2.extraction_attempt_id=ea.id
                             ORDER BY d2.id DESC LIMIT 1)
                            AND dd.status='active'
                          ORDER BY dd.created_at DESC,dd.id DESC
                          LIMIT 1) AS derivation_id,
                       ARRAY(SELECT c.id FROM chunks c
                          WHERE c.document_id=(SELECT d3.id FROM documents d3
                             WHERE d3.extraction_attempt_id=ea.id
                             ORDER BY d3.id DESC LIMIT 1)
                          ORDER BY c.ordinal,c.id LIMIT 101) AS fallback_chunk_ids,
                       (SELECT count(*) FROM chunks c
                          WHERE c.document_id=(SELECT d4.id FROM documents d4
                             WHERE d4.extraction_attempt_id=ea.id
                             ORDER BY d4.id DESC LIMIT 1)) AS fallback_chunk_count,
                       ea.created_at
                FROM extraction_attempts ea
                JOIN research_runs r ON r.id=ea.run_id
                LEFT JOIN research_invocations i ON i.id=ea.invocation_id
                WHERE {predicate} {cursor_predicate}
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
                "source_id",
                "snapshot_id",
                "document_id",
                "derivation_id",
                "fallback_chunk_ids",
                "fallback_chunk_count",
                "created_at",
            ),
        )
        item_map = _invocation_item_map(
            connection,
            [UUID(str(row["invocation_id"])) for row in rows if row["invocation_id"]],
        )
    for row in rows:
        _apply_attempt_result(row, item_map.get(UUID(str(row["id"]))))
    result = _page("extraction_attempts", rows, page.limit, scope=scope)
    result.update(scope_result)
    return _finalize_payload(result, max_chars=_MAX_HISTORY_OUTPUT_CHARS)
