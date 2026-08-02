"""Authoritative corpus identity, passage, and lexical inspection operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .inspection_contract import (
    _SCHEMA_VERSION,
    InspectionNotFoundError,
    PassageBounds,
    _bound_passage_rows,
    _decode_chunk_cursor,
    _decode_rank_cursor,
    _encode_chunk_cursor,
    _encode_rank_cursor,
    _json_value,
    _rows,
)


def inspect_asset(service, asset_id: UUID | str) -> dict[str, Any]:
    identifier = UUID(str(asset_id))
    queries: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "candidate",
            """SELECT c.id,c.run_id,r.external_run_id,c.canonical_url,
                      c.original_url,c.title,c.snippet,c.domain,c.backend,
                      c.recurrence_count,c.first_seen_at,c.last_seen_at,c.created_at
               FROM search_candidates c JOIN research_runs r ON r.id=c.run_id
               WHERE c.id=%s""",
            (
                "id",
                "run_id",
                "external_run_id",
                "canonical_url",
                "original_url",
                "title",
                "snippet",
                "domain",
                "backend",
                "recurrence_count",
                "first_seen_at",
                "last_seen_at",
                "created_at",
            ),
        ),
        (
            "search_response",
            """SELECT sr.id,sr.run_id,r.external_run_id,sr.query_text,sr.backend,
                      sr.status,sr.raw_blob_sha256,sr.raw_blob_bytes,sr.mime_type,
                      sr.result_count,sr.idempotency_key,sr.created_at
               FROM search_responses sr JOIN research_runs r ON r.id=sr.run_id
               WHERE sr.id=%s""",
            (
                "id",
                "run_id",
                "external_run_id",
                "query_text",
                "backend",
                "status",
                "raw_blob_sha256",
                "raw_blob_bytes",
                "mime_type",
                "result_count",
                "idempotency_key",
                "created_at",
            ),
        ),
        (
            "extraction_attempt",
            """SELECT ea.id,ea.run_id,r.external_run_id,ea.invocation_id,
                      ea.candidate_id,ea.attempt_number,ea.method::text,
                      ea.exit_status::text,ea.raw_blob_sha256,
                      ea.failure_class::text,ea.retry_parent_id,
                      ea.disposition::text,ea.selected,ea.created_at
               FROM extraction_attempts ea JOIN research_runs r ON r.id=ea.run_id
               WHERE ea.id=%s""",
            (
                "id",
                "run_id",
                "external_run_id",
                "invocation_id",
                "candidate_id",
                "attempt_number",
                "method",
                "exit_status",
                "raw_blob_sha256",
                "failure_class",
                "retry_parent_id",
                "disposition",
                "selected",
                "created_at",
            ),
        ),
        (
            "source",
            """SELECT id,canonical_url,registered_domain,source_type,
                      first_seen_at,last_seen_at,default_authority_class,metadata
               FROM sources WHERE id=%s""",
            (
                "id",
                "canonical_url",
                "registered_domain",
                "source_type",
                "first_seen_at",
                "last_seen_at",
                "default_authority_class",
                "metadata",
            ),
        ),
        (
            "snapshot",
            """SELECT s.id,s.source_id,s.extraction_attempt_id,s.requested_url,
                      s.final_url,s.retrieved_at,s.http_status,s.mime_type,
                      s.content_sha256,s.raw_blob_uri,s.raw_byte_length,
                      s.firecrawl_version,s.parent_snapshot_id
               FROM asset_snapshots s WHERE s.id=%s""",
            (
                "id",
                "source_id",
                "extraction_attempt_id",
                "requested_url",
                "final_url",
                "retrieved_at",
                "http_status",
                "mime_type",
                "content_sha256",
                "raw_blob_uri",
                "raw_byte_length",
                "firecrawl_version",
                "parent_snapshot_id",
            ),
        ),
        (
            "document",
            """SELECT id,snapshot_id,extraction_attempt_id,title,author,published_at,
                      language,parser_name,parser_version,normalization_version,
                      document_sha256,metadata
               FROM documents WHERE id=%s""",
            (
                "id",
                "snapshot_id",
                "extraction_attempt_id",
                "title",
                "author",
                "published_at",
                "language",
                "parser_name",
                "parser_version",
                "normalization_version",
                "document_sha256",
                "metadata",
            ),
        ),
        (
            "chunk",
            """SELECT id,document_id,first_block_id,last_block_id,ordinal,
                      token_count,content_sha256,chunker_name,chunker_version,
                      tokenizer_name,metadata
               FROM chunks WHERE id=%s""",
            (
                "id",
                "document_id",
                "first_block_id",
                "last_block_id",
                "ordinal",
                "token_count",
                "content_sha256",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "metadata",
            ),
        ),
    )
    found: list[dict[str, Any]] = []
    with service.connection_factory() as connection, connection.cursor() as cursor:
        for asset_type, statement, names in queries:
            cursor.execute(statement, (identifier,))
            row = cursor.fetchone()
            if row is not None:
                found.append(
                    {"asset_type": asset_type, **dict(zip(names, row, strict=True))}
                )
    if not found:
        raise InspectionNotFoundError(f"asset not found: {identifier}")
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "asset_inspection",
        "asset_id": str(identifier),
        "matches": [_json_value(item) for item in found],
        "match_count": len(found),
    }


def passages(
    service, asset_id: UUID | str, bounds: PassageBounds | None = None
) -> dict[str, Any]:
    bounds = bounds or PassageBounds()
    identifier = UUID(str(asset_id))
    marker = _decode_chunk_cursor("passages", bounds.cursor)
    marker_sql = ""
    params: list[Any] = [identifier] * 7
    if marker is not None:
        marker_sql = "AND (c.document_id,c.ordinal,c.id) > (%s,%s,%s)"
        params.extend(marker)
    params.append(bounds.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""WITH target_documents AS (
                    SELECT d.id
                    FROM documents d
                    JOIN asset_snapshots s ON s.id=d.snapshot_id
                    LEFT JOIN extraction_attempts ea
                      ON ea.id=d.extraction_attempt_id
                         OR ea.id=s.extraction_attempt_id
                    WHERE d.id=%s OR d.snapshot_id=%s OR s.source_id=%s
                       OR ea.id=%s OR ea.candidate_id=%s
                    UNION
                    SELECT c0.document_id FROM chunks c0 WHERE c0.id=%s
                    UNION
                    SELECT d0.id FROM document_derivations dd
                    JOIN documents d0 ON d0.id=dd.document_id
                    WHERE dd.id=%s
                )
                SELECT c.id,c.document_id,d.snapshot_id,s.source_id,
                       COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                         AS extraction_attempt_id,
                       ea.candidate_id,c.ordinal,c.token_count,c.content_sha256,
                       c.text
                FROM chunks c
                JOIN target_documents td ON td.id=c.document_id
                JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots s ON s.id=d.snapshot_id
                LEFT JOIN extraction_attempts ea
                  ON ea.id=COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                WHERE true {marker_sql}
                ORDER BY c.document_id,c.ordinal,c.id
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "document_id",
                "snapshot_id",
                "source_id",
                "extraction_attempt_id",
                "candidate_id",
                "ordinal",
                "token_count",
                "content_sha256",
                "text",
            ),
        )
    selected, chars, tokens, exhausted_by = _bound_passage_rows(rows, bounds)
    truncated = len(rows) > len(selected) or exhausted_by is not None
    next_cursor = None
    if truncated and selected:
        last = selected[-1]
        next_cursor = _encode_chunk_cursor(
            "passages",
            UUID(str(last["document_id"])),
            int(last["ordinal"]),
            UUID(str(last["id"])),
        )
    if not selected and not rows:
        raise InspectionNotFoundError(
            f"no passages resolve from authoritative asset: {identifier}"
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "passages",
        "asset_id": str(identifier),
        "items": [_json_value(item) for item in selected],
        "item_count": len(selected),
        "returned_chars": chars,
        "returned_tokens": tokens,
        "truncated": truncated,
        "exhausted_by": exhausted_by,
        "next_cursor": next_cursor,
    }


def lexical_search(
    service,
    query: str,
    *,
    run: UUID | str | None = None,
    bounds: PassageBounds | None = None,
) -> dict[str, Any]:
    bounds = bounds or PassageBounds()
    if not query.strip():
        raise ValueError("query is required")
    run_id = None
    external_id = None
    if run is not None:
        run_id, external_id = service._resolve_run(run)
    marker = _decode_rank_cursor("lexical_search", bounds.cursor)
    filters: list[str] = [
        "c.search_vector @@ plainto_tsquery('simple',%s)",
    ]
    params: list[Any] = [query, query]
    if run_id is not None:
        filters.append("rra.run_id=%s")
        params.append(run_id)
    if marker is not None:
        filters.append(
            "(ts_rank_cd(c.search_vector,plainto_tsquery('simple',%s)),c.id) < (%s,%s)"
        )
        params.extend((query, marker[0], marker[1]))
    params.append(bounds.limit + 1)
    where = " AND ".join(filters)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT c.id,c.document_id,d.snapshot_id,s.source_id,
                       COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                         AS extraction_attempt_id,
                       ea.candidate_id,c.ordinal,c.token_count,c.content_sha256,
                       ts_rank_cd(c.search_vector,
                         plainto_tsquery('simple',%s)) AS rank,
                       c.text
                FROM chunks c
                JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots s ON s.id=d.snapshot_id
                LEFT JOIN extraction_attempts ea
                  ON ea.id=COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                LEFT JOIN research_run_assets rra ON rra.snapshot_id=s.id
                WHERE {where}
                GROUP BY c.id,d.id,s.id,ea.id
                ORDER BY rank DESC,c.id DESC
                LIMIT %s""",
            params,
        )
        rows = _rows(
            cursor,
            (
                "id",
                "document_id",
                "snapshot_id",
                "source_id",
                "extraction_attempt_id",
                "candidate_id",
                "ordinal",
                "token_count",
                "content_sha256",
                "rank",
                "text",
            ),
        )
    selected, chars, tokens, exhausted_by = _bound_passage_rows(rows, bounds)
    truncated = len(rows) > len(selected) or exhausted_by is not None
    next_cursor = None
    if truncated and selected:
        last = selected[-1]
        next_cursor = _encode_rank_cursor(
            "lexical_search", float(last["rank"]), UUID(str(last["id"]))
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "lexical_search",
        "query": query,
        "run_id": str(run_id) if run_id else None,
        "external_run_id": external_id,
        "items": [_json_value(item) for item in selected],
        "item_count": len(selected),
        "returned_chars": chars,
        "returned_tokens": tokens,
        "truncated": truncated,
        "exhausted_by": exhausted_by,
        "next_cursor": next_cursor,
    }
