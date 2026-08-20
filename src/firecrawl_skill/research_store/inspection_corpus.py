"""Authoritative corpus identity, passage, and search inspection operations."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from .inspection_contract import (
    _MAX_INSPECTION_OUTPUT_CHARS,
    _SCHEMA_VERSION,
    InspectionNotFoundError,
    PassageBounds,
    _bound_passage_rows,
    _bounded_identities,
    _bounded_json,
    _bounded_text,
    _decode_chunk_cursor,
    _decode_rank_cursor,
    _encode_chunk_cursor,
    _encode_rank_cursor,
    _finalize_payload,
    _json_value,
    _rows,
    _scope_fingerprint,
)


def inspect_asset(service, asset_id: UUID | str) -> dict[str, Any]:
    identifier = UUID(str(asset_id))
    queries: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "candidate",
            """SELECT c.id,c.run_id,r.external_run_id,c.canonical_url,
                      c.original_url,c.title,c.snippet,c.domain,c.backend,
                      c.recurrence_count,c.first_seen_at,c.last_seen_at,c.created_at,
                      c.backend_metadata
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
                "backend_metadata",
            ),
        ),
        (
            "search_response",
            """SELECT sr.id,sr.run_id,r.external_run_id,sr.query_text,sr.backend,
                      sr.status,sr.raw_blob_sha256,sr.raw_blob_bytes,sr.mime_type,
                      sr.result_count,sr.idempotency_key,sr.created_at,
                      sr.error_message,sr.transport_metadata,sr.payload_summary
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
                "error_message",
                "transport_metadata",
                "payload_summary",
            ),
        ),
        (
            "extraction_attempt",
            """SELECT ea.id,ea.run_id,r.external_run_id,ea.invocation_id,
                      ea.candidate_id,ea.attempt_number,ea.method::text,
                      ea.exit_status::text,ea.raw_blob_sha256,
                      ea.failure_class::text,ea.retry_parent_id,
                      ea.disposition::text,ea.selected,ea.created_at,
                      ea.error_message,ea.quality_metrics
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
                "error_message",
                "quality_metrics",
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
                      s.firecrawl_version,s.parent_snapshot_id,s.crawl_options
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
                "crawl_options",
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
            "derivation",
            """SELECT id,document_id,snapshot_id,status::text,parser_version,
                      normalization_version,chunker_name,chunker_version,
                      tokenizer_name,chunk_count,block_count,error_message,
                      configuration_sha256,created_at
               FROM document_derivations WHERE id=%s""",
            (
                "id",
                "document_id",
                "snapshot_id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "chunk_count",
                "block_count",
                "error_message",
                "configuration_sha256",
                "created_at",
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
        cursor.execute(
            """SELECT item
               FROM research_invocations i
               CROSS JOIN LATERAL jsonb_array_elements(
                 COALESCE(i.output->'items','[]'::jsonb)
               ) AS item
               WHERE item->>'extraction_attempt_id'=%s
               ORDER BY i.created_at DESC LIMIT 1""",
            (str(identifier),),
        )
        attempt_result = cursor.fetchone()
    if not found:
        raise InspectionNotFoundError(f"asset not found: {identifier}")
    for item in found:
        for name, field_value in list(item.items()):
            if isinstance(field_value, str):
                item[name] = _bounded_text(field_value)
            elif isinstance(field_value, (dict, list, tuple)):
                item[name] = _bounded_json(field_value)
        if item["asset_type"] == "extraction_attempt" and attempt_result:
            result = dict(attempt_result[0])
            chunks = list(result.pop("chunk_ids", ()) or ())
            result["chunk_ids"] = _bounded_identities(chunks)
            result["error"] = _bounded_text(result.get("error"))
            result["diagnostic"] = _bounded_text(result.get("diagnostic"))
            item["authoritative_result"] = result
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "asset_inspection",
            "asset_id": str(identifier),
            "matches": [_json_value(item) for item in found],
            "match_count": len(found),
        },
        max_chars=_MAX_INSPECTION_OUTPUT_CHARS,
    )


def _target_documents_sql() -> str:
    return """WITH output_items AS (
                SELECT i.run_id,item
                FROM research_invocations i
                CROSS JOIN LATERAL jsonb_array_elements(
                  COALESCE(i.output->'items','[]'::jsonb)
                ) AS item
                WHERE i.operation='direct_scrape'
            ), target_documents AS (
                SELECT d.id AS document_id,NULL::uuid AS extraction_attempt_id,
                       NULL::uuid AS candidate_id
                FROM documents d
                JOIN asset_snapshots s ON s.id=d.snapshot_id
                WHERE d.id=%s OR d.snapshot_id=%s OR s.source_id=%s
                UNION ALL
                SELECT c0.document_id,NULL::uuid,NULL::uuid
                FROM chunks c0 WHERE c0.id=%s
                UNION ALL
                SELECT dd.document_id,NULL::uuid,NULL::uuid
                FROM document_derivations dd WHERE dd.id=%s
                UNION ALL
                SELECT NULLIF(oi.item->>'document_id','')::uuid,
                       NULLIF(oi.item->>'extraction_attempt_id','')::uuid,
                       NULLIF(oi.item->>'candidate_id','')::uuid
                FROM output_items oi
                WHERE oi.item->>'extraction_attempt_id'=%s::text
                   OR oi.item->>'candidate_id'=%s::text
                   OR oi.item->>'source_id'=%s::text
                   OR oi.item->>'snapshot_id'=%s::text
                   OR oi.item->>'document_id'=%s::text
                   OR oi.item->>'derivation_id'=%s::text
                UNION ALL
                SELECT d.id,ea.id,ea.candidate_id
                FROM extraction_attempts ea
                JOIN asset_snapshots s ON s.extraction_attempt_id=ea.id
                JOIN documents d ON d.snapshot_id=s.id
                WHERE ea.id=%s OR ea.candidate_id=%s
            ), resolved_documents AS (
                SELECT DISTINCT ON (document_id)
                       document_id,extraction_attempt_id,candidate_id
                FROM target_documents
                WHERE document_id IS NOT NULL
                ORDER BY document_id,extraction_attempt_id DESC NULLS LAST,
                         candidate_id DESC NULLS LAST
            )"""


def passages(
    service, asset_id: UUID | str, bounds: PassageBounds | None = None
) -> dict[str, Any]:
    bounds = bounds or PassageBounds()
    identifier = UUID(str(asset_id))
    scope = _scope_fingerprint("passages", asset_id=identifier)
    marker = _decode_chunk_cursor("passages", bounds.cursor, scope=scope)
    marker_sql = ""
    params: list[Any] = [identifier] * 13
    start_offset = 0
    if marker is not None:
        operator = ">=" if marker[3] > 0 else ">"
        marker_sql = f"AND (c.document_id,c.ordinal,c.id) {operator} (%s,%s,%s)"
        params.extend(marker[:3])
        start_offset = marker[3]
    params.append(bounds.limit + 1)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            _target_documents_sql()
            + f"""
                SELECT c.id,c.document_id,d.snapshot_id,s.source_id,
                       COALESCE(rd.extraction_attempt_id,d.extraction_attempt_id,
                                s.extraction_attempt_id) AS extraction_attempt_id,
                       COALESCE(rd.candidate_id,ea.candidate_id) AS candidate_id,
                       c.ordinal,c.token_count,c.content_sha256,c.tokenizer_name,c.text
                FROM chunks c
                JOIN resolved_documents rd ON rd.document_id=c.document_id
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
                "tokenizer_name",
                "text",
            ),
        )
    if (
        marker is not None
        and marker[3] > 0
        and (not rows or UUID(str(rows[0]["id"])) != marker[2])
    ):
        raise ValueError("pagination cursor no longer resolves to its chunk")
    selected, chars, tokens, exhausted_by, resume = _bound_passage_rows(
        rows, bounds, start_offset=start_offset
    )
    has_more = bool(
        resume
        and (
            resume[1] > 0
            or len(rows) > len(selected)
            or exhausted_by in {"limit", "max_chars", "max_tokens"}
        )
    )
    next_cursor = None
    if has_more and resume is not None:
        last, offset = resume
        next_cursor = _encode_chunk_cursor(
            "passages",
            UUID(str(last["document_id"])),
            int(last["ordinal"]),
            UUID(str(last["id"])),
            scope=scope,
            offset=offset,
        )
    if not selected and not rows:
        raise InspectionNotFoundError(
            f"no passages resolve from authoritative asset: {identifier}"
        )
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "passages",
            "asset_id": str(identifier),
            "items": [_json_value(item) for item in selected],
            "item_count": len(selected),
            "returned_chars": chars,
            "returned_tokens": tokens,
            "truncated": has_more,
            "exhausted_by": exhausted_by,
            "next_cursor": next_cursor,
        },
        max_chars=_MAX_INSPECTION_OUTPUT_CHARS,
    )


def _provenance_lateral(run_filtered: bool) -> str:
    run_clause = "AND i.run_id=%s" if run_filtered else ""
    return f"""LEFT JOIN LATERAL (
                SELECT item
                FROM research_invocations i
                CROSS JOIN LATERAL jsonb_array_elements(
                  COALESCE(i.output->'items','[]'::jsonb)
                ) AS item
                WHERE i.operation='direct_scrape'
                  AND item->>'document_id'=d.id::text
                  {run_clause}
                ORDER BY i.created_at DESC,i.id DESC LIMIT 1
              ) oi ON TRUE"""


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
    scope = _scope_fingerprint("lexical_search", query=query, run_id=run_id)
    marker = _decode_rank_cursor("lexical_search", bounds.cursor, scope=scope)
    filters = ["c.search_vector @@ plainto_tsquery('simple',%s)"]
    lateral = _provenance_lateral(run_id is not None)
    params: list[Any] = [query]
    if run_id is not None:
        params.append(run_id)
    params.append(query)
    if run_id is not None:
        filters.append(
            "EXISTS (SELECT 1 FROM research_run_assets rra "
            "WHERE rra.snapshot_id=s.id AND rra.run_id=%s)"
        )
        params.append(run_id)
    start_offset = 0
    if marker is not None:
        operator = "<=" if marker[2] > 0 else "<"
        filters.append(
            "(ts_rank_cd(c.search_vector,plainto_tsquery('simple',%s)),c.id) "
            f"{operator} (%s,%s)"
        )
        params.extend((query, marker[0], marker[1]))
        start_offset = marker[2]
    params.append(bounds.limit + 1)
    where = " AND ".join(filters)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""SELECT c.id,c.document_id,d.snapshot_id,s.source_id,
                       COALESCE(NULLIF(oi.item->>'extraction_attempt_id','')::uuid,
                                d.extraction_attempt_id,s.extraction_attempt_id)
                         AS extraction_attempt_id,
                       COALESCE(NULLIF(oi.item->>'candidate_id','')::uuid,
                                ea.candidate_id) AS candidate_id,
                       c.ordinal,c.token_count,c.content_sha256,c.tokenizer_name,
                       ts_rank_cd(c.search_vector,
                         plainto_tsquery('simple',%s)) AS rank,c.text
                FROM chunks c
                JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots s ON s.id=d.snapshot_id
                LEFT JOIN extraction_attempts ea
                  ON ea.id=COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                {lateral}
                WHERE {where}
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
                "tokenizer_name",
                "rank",
                "text",
            ),
        )
    if (
        marker is not None
        and marker[2] > 0
        and (not rows or UUID(str(rows[0]["id"])) != marker[1])
    ):
        raise ValueError("pagination cursor no longer resolves to its lexical match")
    selected, chars, tokens, exhausted_by, resume = _bound_passage_rows(
        rows, bounds, start_offset=start_offset
    )
    has_more = bool(
        resume
        and (
            resume[1] > 0
            or len(rows) > len(selected)
            or exhausted_by in {"limit", "max_chars", "max_tokens"}
        )
    )
    next_cursor = None
    if has_more and resume is not None:
        last, offset = resume
        next_cursor = _encode_rank_cursor(
            "lexical_search",
            float(last["rank"]),
            UUID(str(last["id"])),
            scope=scope,
            offset=offset,
        )
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "lexical_search",
            "query": query,
            "run_id": str(run_id) if run_id else None,
            "external_run_id": external_id,
            "items": [_json_value(item) for item in selected],
            "item_count": len(selected),
            "returned_chars": chars,
            "returned_tokens": tokens,
            "truncated": has_more,
            "exhausted_by": exhausted_by,
            "next_cursor": next_cursor,
        },
        max_chars=_MAX_INSPECTION_OUTPUT_CHARS,
    )


def pattern_search(
    service,
    pattern: str,
    *,
    mode: str = "literal",
    run: UUID | str | None = None,
    bounds: PassageBounds | None = None,
) -> dict[str, Any]:
    """Bounded case-insensitive literal or regular-expression corpus search."""

    bounds = bounds or PassageBounds()
    if not pattern:
        raise ValueError("pattern is required")
    if mode not in {"literal", "regex"}:
        raise ValueError("mode must be literal or regex")
    if mode == "regex":
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
    run_id = None
    external_id = None
    if run is not None:
        run_id, external_id = service._resolve_run(run)
    scope = _scope_fingerprint(
        "pattern_search", pattern=pattern, mode=mode, run_id=run_id
    )
    marker = _decode_chunk_cursor("pattern_search", bounds.cursor, scope=scope)
    filters = [
        "strpos(lower(c.text),lower(%s)) > 0" if mode == "literal" else "c.text ~* %s"
    ]
    lateral = _provenance_lateral(run_id is not None)
    params: list[Any] = []
    if run_id is not None:
        params.append(run_id)
    params.append(pattern)
    if run_id is not None:
        filters.append(
            "EXISTS (SELECT 1 FROM research_run_assets rra "
            "WHERE rra.snapshot_id=s.id AND rra.run_id=%s)"
        )
        params.append(run_id)
    start_offset = 0
    if marker is not None:
        operator = ">=" if marker[3] > 0 else ">"
        filters.append(f"(c.document_id,c.ordinal,c.id) {operator} (%s,%s,%s)")
        params.extend(marker[:3])
        start_offset = marker[3]
    params.append(bounds.limit + 1)
    where = " AND ".join(filters)
    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '2000ms'")
        cursor.execute(
            f"""SELECT c.id,c.document_id,d.snapshot_id,s.source_id,
                       COALESCE(NULLIF(oi.item->>'extraction_attempt_id','')::uuid,
                                d.extraction_attempt_id,s.extraction_attempt_id)
                         AS extraction_attempt_id,
                       COALESCE(NULLIF(oi.item->>'candidate_id','')::uuid,
                                ea.candidate_id) AS candidate_id,
                       c.ordinal,c.token_count,c.content_sha256,c.tokenizer_name,c.text
                FROM chunks c
                JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots s ON s.id=d.snapshot_id
                LEFT JOIN extraction_attempts ea
                  ON ea.id=COALESCE(d.extraction_attempt_id,s.extraction_attempt_id)
                {lateral}
                WHERE {where}
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
                "tokenizer_name",
                "text",
            ),
        )
    if (
        marker is not None
        and marker[3] > 0
        and (not rows or UUID(str(rows[0]["id"])) != marker[2])
    ):
        raise ValueError("pagination cursor no longer resolves to its match")
    selected, chars, tokens, exhausted_by, resume = _bound_passage_rows(
        rows, bounds, start_offset=start_offset
    )
    has_more = bool(
        resume
        and (
            resume[1] > 0
            or len(rows) > len(selected)
            or exhausted_by in {"limit", "max_chars", "max_tokens"}
        )
    )
    next_cursor = None
    if has_more and resume is not None:
        last, offset = resume
        next_cursor = _encode_chunk_cursor(
            "pattern_search",
            UUID(str(last["document_id"])),
            int(last["ordinal"]),
            UUID(str(last["id"])),
            scope=scope,
            offset=offset,
        )
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "pattern_search",
            "pattern": pattern,
            "mode": mode,
            "run_id": str(run_id) if run_id else None,
            "external_run_id": external_id,
            "items": [_json_value(item) for item in selected],
            "item_count": len(selected),
            "returned_chars": chars,
            "returned_tokens": tokens,
            "truncated": has_more,
            "exhausted_by": exhausted_by,
            "next_cursor": next_cursor,
        },
        max_chars=_MAX_INSPECTION_OUTPUT_CHARS,
    )
