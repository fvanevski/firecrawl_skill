"""Unified read-only provider-operation history for one research run."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .inspection_contract import (
    _MAX_HISTORY_OUTPUT_CHARS,
    _SCHEMA_VERSION,
    PageRequest,
    _bounded_text,
    _decode_payload,
    _encode_payload,
    _finalize_payload,
    _json_value,
    _rows,
    _scope_fingerprint,
)


def _encode_cursor(
    *,
    timestamp: datetime,
    record_id: UUID,
    record_kind: str,
    scope: str,
) -> str:
    return _encode_payload(
        {
            "kind": "operations",
            "scope": scope,
            "timestamp": timestamp.isoformat(),
            "id": str(record_id),
            "record_kind": record_kind,
        }
    )


def _decode_cursor(value: str | None, *, scope: str) -> tuple[datetime, UUID, str] | None:
    payload = _decode_payload("operations", scope, value)
    if payload is None:
        return None
    try:
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        record_id = UUID(str(payload["id"]))
        record_kind = str(payload["record_kind"])
        if record_kind not in {"invocation", "extraction_attempt"}:
            raise ValueError("invalid record kind")
        return timestamp, record_id, record_kind
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def list_operations(
    service: Any,
    run: UUID | str,
    page: PageRequest | None = None,
) -> dict[str, Any]:
    """Return the deterministic union of invocation and extraction-attempt history.

    The two PostgreSQL relations remain independent authorities.  This function
    performs a read-only UNION ALL solely for operator inspection; it never
    inserts, copies, or rewrites either identity type.
    """

    page = page or PageRequest()
    run_id, external_id = service._resolve_run(run)
    scope = _scope_fingerprint("operations", run_id=run_id)
    marker = _decode_cursor(page.cursor, scope=scope)
    marker_where = ""
    marker_params: list[Any] = []
    if marker is not None:
        marker_where = "WHERE (occurred_at,id,record_kind) < (%s,%s,%s)"
        marker_params.extend(marker)

    with service.connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""WITH operation_rows AS (
                    SELECT
                        'invocation'::text AS record_kind,
                        i.id,
                        CASE
                            WHEN lower(i.operation::text) LIKE '%%search%%' THEN 'search'
                            WHEN lower(i.operation::text) LIKE '%%scrape%%' THEN 'scrape'
                            ELSE i.operation::text
                        END AS operation_kind,
                        COALESCE(
                            i.input->>'query_text',
                            i.input->>'query',
                            i.input#>>'{{requests,0,url}}',
                            i.input#>>'{{requests,0,candidate_id}}'
                        ) AS target,
                        i.status::text AS status,
                        COALESCE(i.started_at,i.created_at) AS occurred_at,
                        i.id AS related_invocation_id,
                        NULL::uuid AS related_attempt_id,
                        i.external_invocation_id,
                        NULL::text AS failure_class,
                        i.error::text AS error
                    FROM research_invocations i
                    WHERE i.run_id=%s

                    UNION ALL

                    SELECT
                        'extraction_attempt'::text AS record_kind,
                        ea.id,
                        'scrape'::text AS operation_kind,
                        COALESCE(c.canonical_url,c.original_url) AS target,
                        ea.exit_status::text AS status,
                        COALESCE(ea.start_time,ea.created_at) AS occurred_at,
                        ea.invocation_id AS related_invocation_id,
                        ea.id AS related_attempt_id,
                        i.external_invocation_id,
                        ea.failure_class::text AS failure_class,
                        ea.error_message::text AS error
                    FROM extraction_attempts ea
                    LEFT JOIN search_candidates c
                      ON c.id=ea.candidate_id AND c.run_id=ea.run_id
                    LEFT JOIN research_invocations i ON i.id=ea.invocation_id
                    WHERE ea.run_id=%s
                )
                SELECT record_kind,id,operation_kind,target,status,occurred_at,
                       related_invocation_id,related_attempt_id,
                       external_invocation_id,failure_class,error
                FROM operation_rows
                {marker_where}
                ORDER BY occurred_at DESC,id DESC,record_kind DESC
                LIMIT %s""",
            (run_id, run_id, *marker_params, page.limit + 1),
        )
        rows = _rows(
            cursor,
            (
                "record_kind",
                "id",
                "operation_kind",
                "target",
                "status",
                "occurred_at",
                "related_invocation_id",
                "related_attempt_id",
                "external_invocation_id",
                "failure_class",
                "error",
            ),
        )

    for row in rows:
        row["target"] = _bounded_text(row.get("target"))
        row["error"] = _bounded_text(row.get("error"))

    selected = rows[: page.limit]
    truncated = len(rows) > page.limit
    next_cursor = None
    if truncated and selected:
        last = selected[-1]
        next_cursor = _encode_cursor(
            timestamp=last["occurred_at"],
            record_id=UUID(str(last["id"])),
            record_kind=str(last["record_kind"]),
            scope=scope,
        )
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "operations",
            "run_id": str(run_id),
            "external_run_id": external_id,
            "items": [_json_value(item) for item in selected],
            "item_count": len(selected),
            "truncated": truncated,
            "next_cursor": next_cursor,
        },
        max_chars=_MAX_HISTORY_OUTPUT_CHARS,
    )


__all__ = ["list_operations"]
