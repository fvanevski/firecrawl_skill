"""Bounded, secret-safe, one-snapshot run exports for ARC-15 / issue #221."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from .index_census import CENSUS_CLASSES, census_index_jobs
from .service import json_default

EXPORT_RUN_SCHEMA_VERSIONS = ("export-run-v1", "export-run-v2")
INTEGRITY_SCHEMA_VERSIONS = ("integrity-v1",)
SECTION_ITEM_LIMIT = 50
NESTED_ITEM_LIMIT = 25
MAPPING_ITEM_LIMIT = 100
TEXT_CHARACTER_LIMIT = 2_000
_REDACTED = "***REDACTED***"

_SECRET_NAME = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization|"
    r"password|secret|credential|awsaccesskeyid|x-amz-credential|signature|"
    r"x-amz-signature|token"
)
_SECRET_KEY = re.compile(rf"^(?:{_SECRET_NAME})$", re.IGNORECASE)
_SECRET_QUERY = re.compile(rf"(?i)([?&](?:{_SECRET_NAME})=)([^&#\s]+)")
_SECRET_ASSIGNMENT = re.compile(rf"(?i)((?:{_SECRET_NAME})\s*[=:]\s*)([^\s&,;]+)")
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_URI_USERINFO = re.compile(r"(://[^/@:\s]+:)[^/@\s]+@")
_UNIX_HOME = re.compile(r"/(?:home|Users)/[^/\s]+")
_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=json_default,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _redact_text(value: str) -> str:
    value = _SECRET_QUERY.sub(lambda match: match.group(1) + _REDACTED, value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + _REDACTED, value)
    value = _BEARER.sub(lambda match: f"{match.group(1)} {_REDACTED}", value)
    value = _URI_USERINFO.sub(lambda match: match.group(1) + _REDACTED + "@", value)
    value = _UNIX_HOME.sub("/***REDACTED_HOME***", value)
    return _WINDOWS_HOME.sub(r"C:\\***REDACTED_HOME***", value)


def _bounded_mapping(value: Mapping[Any, Any]) -> Any:
    entries: list[tuple[str, Any]] = []
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        key_text = str(key)
        safe = _REDACTED if _SECRET_KEY.fullmatch(key_text.strip()) else _safe(item)
        entries.append((key_text, safe))
    if len(entries) <= MAPPING_ITEM_LIMIT:
        return dict(entries)
    return {
        "schema_version": "bounded-map-v1",
        "exact_count": len(entries),
        "items": dict(entries[:MAPPING_ITEM_LIMIT]),
        "items_limit": MAPPING_ITEM_LIMIT,
        "sha256": _sha(dict(entries)),
        "truncated": True,
    }


def _safe(value: Any) -> Any:
    """Recursively redact secrets and bound nested collections/text."""
    if isinstance(value, Mapping):
        return _bounded_mapping(value)
    if isinstance(value, (list, tuple)):
        safe = [_safe(item) for item in value]
        if len(safe) <= NESTED_ITEM_LIMIT:
            return safe
        return {
            "schema_version": "bounded-list-v1",
            "exact_count": len(safe),
            "items": safe[:NESTED_ITEM_LIMIT],
            "items_limit": NESTED_ITEM_LIMIT,
            "sha256": _sha(safe),
            "truncated": True,
        }
    if isinstance(value, str):
        value = _redact_text(value)
        if len(value) <= TEXT_CHARACTER_LIMIT:
            return value
        return {
            "schema_version": "bounded-text-v1",
            "character_count": len(value),
            "prefix": value[:TEXT_CHARACTER_LIMIT],
            "prefix_character_limit": TEXT_CHARACTER_LIMIT,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "truncated": True,
        }
    return value


def _empty() -> dict[str, Any]:
    return {
        "exact_count": 0,
        "items": [],
        "items_limit": SECTION_ITEM_LIMIT,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "truncated": False,
    }


def _section(
    cursor: Any,
    sql: str,
    params: Sequence[Any] = (),
    *,
    limit: int = SECTION_ITEM_LIMIT,
) -> dict[str, Any]:
    """Return exact count/hash plus a bounded prefix in deterministic SQL order."""
    cursor.execute(sql, tuple(params))
    digest = hashlib.sha256()
    items: list[Any] = []
    count = 0
    for row in cursor:
        item = _safe(row[0])
        encoded = _canonical(item).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
        if len(items) < limit:
            items.append(item)
    return {
        "exact_count": count,
        "items": items,
        "items_limit": limit,
        "sha256": digest.hexdigest(),
        "truncated": count > limit,
    }


def _resolve_run(cursor: Any, identifier: str) -> dict[str, Any]:
    try:
        run_id = UUID(identifier)
    except ValueError:
        cursor.execute(
            "SELECT row_to_json(r) FROM research_runs r WHERE external_run_id=%s",
            (identifier,),
        )
    else:
        cursor.execute(
            "SELECT row_to_json(r) FROM research_runs r WHERE id=%s", (run_id,)
        )
    row = cursor.fetchone()
    if row is None:
        raise SystemExit("research run not found")
    return row[0]


def _schema_version(cursor: Any) -> str | None:
    cursor.execute("SELECT version_num FROM alembic_version")
    row = cursor.fetchone()
    return None if row is None else str(row[0])


def _transaction(cursor: Any) -> dict[str, Any]:
    cursor.execute(
        "SELECT transaction_timestamp(),current_setting('transaction_isolation'),"
        "current_setting('transaction_read_only')"
    )
    observed_at, isolation, read_only = cursor.fetchone()
    return {
        "observed_at": observed_at,
        "isolation": str(isolation),
        "read_only": str(read_only).lower() == "on",
    }


def _membership(cursor: Any, run_id: UUID) -> tuple[dict[str, Any], list[UUID]]:
    cursor.execute(
        """SELECT row_to_json(s) FROM run_asset_membership_seals s
           WHERE s.run_id=%s AND s.status='sealed'
           ORDER BY s.seal_revision DESC,s.id DESC LIMIT 1""",
        (run_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return {
            "active_seal": None,
            "members": _empty(),
            "exact_chunk_count": 0,
            "exact_chunk_ids_sha256": _sha([]),
        }, []

    seal = row[0]
    seal_id = UUID(str(seal["id"]))
    members = _section(
        cursor,
        """SELECT row_to_json(m) FROM run_asset_membership_members m
           WHERE m.seal_id=%s ORDER BY m.ordinal,m.subject_id""",
        (seal_id,),
    )
    cursor.execute(
        """SELECT DISTINCT chunk_id FROM run_asset_membership_members m,
           unnest(m.chunk_ids) AS chunk_id WHERE m.seal_id=%s ORDER BY chunk_id""",
        (seal_id,),
    )
    chunk_ids = [UUID(str(row[0])) for row in cursor.fetchall()]
    return {
        "active_seal": _safe(seal),
        "members": members,
        "exact_chunk_count": len(chunk_ids),
        "exact_chunk_ids_sha256": _sha([str(item) for item in chunk_ids]),
    }, chunk_ids


def _checkpoint(
    cursor: Any,
    run_id: UUID,
    seal_id: UUID | None,
) -> tuple[dict[str, Any] | None, list[UUID], str | None]:
    if seal_id is None:
        cursor.execute(
            """SELECT row_to_json(c) FROM indexing_checkpoints c
               WHERE c.run_id=%s ORDER BY c.created_at DESC,c.id DESC LIMIT 1""",
            (run_id,),
        )
    else:
        cursor.execute(
            """SELECT row_to_json(c) FROM indexing_checkpoints c
               WHERE c.run_id=%s AND c.asset_membership_seal_id=%s
               ORDER BY c.created_at DESC,c.id DESC LIMIT 1""",
            (run_id, seal_id),
        )
    row = cursor.fetchone()
    if row is None:
        return None, [], None
    checkpoint = row[0]
    entity_ids = sorted(
        {UUID(str(item)) for item in checkpoint.get("entity_ids") or ()},
        key=str,
    )
    fingerprint = checkpoint.get("fingerprint")
    return _safe(checkpoint), entity_ids, str(fingerprint) if fingerprint else None


def _index_sections(
    connection: Any,
    cursor: Any,
    entity_ids: Sequence[UUID],
    fingerprint: str | None,
    max_attempts: int,
) -> dict[str, Any]:
    if not entity_ids:
        return {
            "census": None,
            "index_jobs": _empty(),
            "embedding_manifests": _empty(),
            "active_leases": _empty(),
            "heartbeats": _empty(),
        }
    ids = list(entity_ids)
    jobs = _section(
        cursor,
        """SELECT row_to_json(j) FROM index_jobs j
           WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
           ORDER BY j.entity_id,j.id""",
        (ids,),
    )
    manifests = _section(
        cursor,
        """SELECT row_to_json(m) FROM embedding_manifests m
           WHERE m.chunk_id=ANY(%s::uuid[]) ORDER BY m.chunk_id,m.id""",
        (ids,),
    )
    leases = _section(
        cursor,
        """SELECT row_to_json(x) FROM (
             SELECT j.id AS job_id,j.entity_id,j.manifest_id,j.index_definition_id,
                    j.status,j.attempt_count,j.lease_owner,j.lease_expires_at
             FROM index_jobs j
             WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
               AND j.lease_token IS NOT NULL ORDER BY j.entity_id,j.id
           ) x""",
        (ids,),
    )
    heartbeats = _section(
        cursor,
        """SELECT row_to_json(h) FROM index_worker_heartbeats h
           WHERE h.worker_id IN (
             SELECT DISTINCT j.lease_owner FROM index_jobs j
             WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
               AND j.lease_owner IS NOT NULL
           ) ORDER BY h.worker_id""",
        (ids,),
    )
    census = None
    if fingerprint:
        census = _safe(
            census_index_jobs(
                connection,
                ids,
                fingerprint,
                max_attempts=max_attempts,
                representative_limit=SECTION_ITEM_LIMIT,
            )
        )
    return {
        "census": census,
        "index_jobs": jobs,
        "embedding_manifests": manifests,
        "active_leases": leases,
        "heartbeats": heartbeats,
    }


def _terminal_timing(
    cursor: Any,
    run_id: UUID,
    entity_ids: Sequence[UUID],
    fingerprint: str | None,
) -> dict[str, Any]:
    cursor.execute(
        """SELECT row_to_json(d) FROM terminal_decisions d
           WHERE d.run_id=%s ORDER BY d.created_at DESC,d.id DESC LIMIT 1""",
        (run_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return {
            "terminal_decision": None,
            "completed_after_decision": _empty(),
            "spanning_terminal_decision_exact_count": 0,
            "historical_identity_correlation": {
                "status": "inconclusive",
                "reason_code": "no_terminal_decision",
            },
        }
    decision = row[0]
    decision_at = decision.get("created_at")
    if not entity_ids or decision_at is None:
        return {
            "terminal_decision": _safe(decision),
            "completed_after_decision": _empty(),
            "spanning_terminal_decision_exact_count": 0,
            "historical_identity_correlation": {
                "status": "inconclusive",
                "reason_code": "no_exact_membership_for_terminal_correlation",
            },
        }

    fingerprint_sql = " AND d.fingerprint=%s" if fingerprint else ""
    completed_params: list[Any] = [
        decision_at,
        decision_at,
        list(entity_ids),
        decision_at,
    ]
    if fingerprint:
        completed_params.append(fingerprint)
    completed = _section(
        cursor,
        """SELECT row_to_json(x) FROM (
             SELECT j.id AS job_id,j.entity_id,j.manifest_id,j.index_definition_id,
                    j.status,j.attempt_count,j.started_at,j.completed_at,
                    (j.started_at IS NOT NULL AND j.started_at<=%s
                     AND j.completed_at>%s) AS spans_terminal_decision
             FROM index_jobs j
             JOIN embedding_manifests m ON m.id=j.manifest_id
             JOIN index_definitions d ON d.id=m.index_definition_id
             WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
               AND j.completed_at>%s"""
        + fingerprint_sql
        + " ORDER BY j.completed_at,j.entity_id,j.id) x",
        completed_params,
    )
    count_params: list[Any] = [list(entity_ids), decision_at, decision_at]
    if fingerprint:
        count_params.append(fingerprint)
    cursor.execute(
        """SELECT count(*) FROM index_jobs j
           JOIN embedding_manifests m ON m.id=j.manifest_id
           JOIN index_definitions d ON d.id=m.index_definition_id
           WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
             AND j.started_at IS NOT NULL AND j.started_at<=%s
             AND j.completed_at>%s"""
        + fingerprint_sql,
        tuple(count_params),
    )
    spanning = int(cursor.fetchone()[0])
    terminal_census = decision.get("state_census")
    running_live = None
    if isinstance(terminal_census, Mapping):
        counts = terminal_census.get("counts")
        if isinstance(counts, Mapping):
            running_live = counts.get("running_live")
        elif isinstance(terminal_census.get("running_live"), int):
            running_live = terminal_census.get("running_live")
    return {
        "terminal_decision": _safe(decision),
        "completed_after_decision": completed,
        "spanning_terminal_decision_exact_count": spanning,
        "terminal_census_running_live_count": running_live,
        "historical_identity_correlation": {
            "status": "inconclusive",
            "reason_code": "terminal_census_does_not_persist_full_running_live_id_set",
            "note": (
                "Persisted terminal counts and later exact job timing are reported "
                "separately; unsupported historical per-job lease provenance is not inferred."
            ),
        },
    }


def _projection(
    cursor: Any,
    entity_ids: Sequence[UUID],
    fingerprint: str | None,
) -> dict[str, Any]:
    if not fingerprint:
        return {
            "status": "inconclusive",
            "reason_code": "no_persisted_index_fingerprint",
            "authoritative_for_completion": False,
            "definitions": _empty(),
            "point_count_cache": _empty(),
        }
    definitions = _section(
        cursor,
        """SELECT row_to_json(d) FROM index_definitions d
           WHERE d.fingerprint=%s ORDER BY d.created_at,d.id""",
        (fingerprint,),
    )
    point_counts = _section(
        cursor,
        """SELECT row_to_json(p) FROM index_point_counts p
           WHERE p.index_definition_id IN (
             SELECT id FROM index_definitions WHERE fingerprint=%s
           ) ORDER BY p.last_verified_at,p.index_definition_id""",
        (fingerprint,),
    )
    if not entity_ids:
        return {
            "status": "inconclusive",
            "reason_code": "no_exact_membership_for_projection_reconciliation",
            "authoritative_for_completion": False,
            "definitions": definitions,
            "point_count_cache": point_counts,
        }
    cursor.execute(
        """SELECT count(*) FILTER(WHERE m.index_status='complete'),count(*)
           FROM embedding_manifests m JOIN index_definitions d ON d.id=m.index_definition_id
           WHERE d.fingerprint=%s AND m.chunk_id=ANY(%s::uuid[])""",
        (fingerprint, list(entity_ids)),
    )
    complete, total = cursor.fetchone()
    return {
        "status": "inconclusive",
        "reason_code": "live_qdrant_not_queried_by_offline_export",
        "authoritative_for_completion": False,
        "expected_membership_chunk_count": len(entity_ids),
        "matching_manifest_count": int(total),
        "complete_manifest_count": int(complete),
        "definitions": definitions,
        "point_count_cache": point_counts,
        "note": "PostgreSQL remains authoritative; Qdrant is projection-only evidence.",
    }


def _terminal_counts(terminal: Mapping[str, Any]) -> Mapping[str, Any] | None:
    census = terminal.get("state_census")
    if not isinstance(census, Mapping) or census.get("available") is False:
        return None
    counts = census.get("counts")
    return counts if isinstance(counts, Mapping) else census


def _noncomplete(counts: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in CENSUS_CLASSES:
        if name == "complete":
            continue
        value = counts.get(name, 0)
        if isinstance(value, int) and value > 0:
            result[name] = value
    return result


def _diagnostics(
    run: Mapping[str, Any],
    membership: Mapping[str, Any],
    seal_ids: Sequence[UUID],
    checkpoint: Mapping[str, Any] | None,
    checkpoint_ids: Sequence[UUID],
    census: Mapping[str, Any] | None,
    timing: Mapping[str, Any],
    unresolved_search: int,
    completion_counts: Mapping[str, int],
) -> dict[str, Any]:
    domains: dict[str, dict[str, Any]] = {}
    seal = membership.get("active_seal")
    if seal is None:
        domains["membership"] = {
            "status": "inconclusive",
            "reason_code": (
                "legacy_checkpoint_without_asset_membership_seal"
                if checkpoint is not None
                else "no_sealed_membership_or_checkpoint"
            ),
        }
    else:
        expected_assets = int(seal.get("expected_asset_count", -1))
        expected_chunks = int(seal.get("expected_chunk_count", -1))
        mismatch = (
            expected_assets != membership["members"]["exact_count"]
            or expected_chunks != membership["exact_chunk_count"]
            or bool(checkpoint_ids and set(checkpoint_ids) != set(seal_ids))
        )
        domains["membership"] = {
            "status": "failure" if mismatch else "pass",
            "reason_code": (
                "sealed_membership_checkpoint_or_count_mismatch"
                if mismatch
                else "sealed_membership_consistent"
            ),
            "expected_asset_count": expected_assets,
            "observed_asset_count": membership["members"]["exact_count"],
            "expected_chunk_count": expected_chunks,
            "observed_chunk_count": membership["exact_chunk_count"],
        }

    if census is None:
        domains["indexing"] = {
            "status": "inconclusive",
            "reason_code": "exact_index_census_unavailable",
        }
    else:
        counts = census.get("counts")
        counts = counts if isinstance(counts, Mapping) else census
        noncomplete = _noncomplete(counts)
        domains["indexing"] = {
            "status": "failure" if noncomplete else "pass",
            "reason_code": (
                "index_membership_incomplete"
                if noncomplete
                else "index_membership_complete"
            ),
            "noncomplete_classes": noncomplete,
            "decision_evidence": census.get("decision_evidence"),
        }

    terminal = timing.get("terminal_decision")
    if not isinstance(terminal, Mapping):
        domains["terminal_decision"] = {
            "status": "inconclusive",
            "reason_code": "no_terminal_decision",
        }
    else:
        counts = _terminal_counts(terminal)
        if counts is None:
            domains["terminal_decision"] = {
                "status": "inconclusive",
                "reason_code": "terminal_census_unavailable_or_missing",
            }
        else:
            noncomplete = _noncomplete(counts)
            domains["terminal_decision"] = {
                "status": "failure" if noncomplete else "pass",
                "reason_code": (
                    "terminal_decision_census_contains_noncomplete_index_work"
                    if noncomplete
                    else "terminal_decision_census_complete"
                ),
                "noncomplete_classes": noncomplete,
                "later_completion_count": timing["completed_after_decision"][
                    "exact_count"
                ],
                "spanning_terminal_decision_exact_count": timing[
                    "spanning_terminal_decision_exact_count"
                ],
            }

    domains["search_provenance"] = {
        "status": "inconclusive" if unresolved_search else "pass",
        "reason_code": (
            "search_provenance_contains_explicitly_unresolved_history"
            if unresolved_search
            else "search_provenance_resolved_or_not_applicable"
        ),
        "unresolved_response_count": unresolved_search,
    }

    if run.get("state") == "completed":
        required = (
            "evidence_packets",
            "semantic_calls",
            "semantic_artifacts",
            "synthesis_stages",
        )
        missing = [name for name in required if completion_counts.get(name, 0) <= 0]
        domains["synthesis_completion"] = {
            "status": "failure" if missing else "pass",
            "reason_code": (
                "completed_run_missing_persisted_completion_provenance"
                if missing
                else "completed_run_has_persisted_completion_provenance"
            ),
            "missing_evidence_domains": missing,
            "note": "The production completion guard remains the semantic authority.",
        }
    else:
        domains["synthesis_completion"] = {
            "status": "inconclusive",
            "reason_code": "noncompleted_run_has_no_required_authoritative_completion",
        }

    statuses = {domain["status"] for domain in domains.values()}
    overall = (
        "failure"
        if "failure" in statuses
        else "inconclusive"
        if "inconclusive" in statuses
        else "pass"
    )
    return {"overall_status": overall, "domains": domains}


def _core_sections(cursor: Any, run_id: UUID) -> dict[str, dict[str, Any]]:
    specs = {
        "lifecycle_ledger": (
            (
                "SELECT row_to_json(t) FROM research_run_transitions t WHERE t.run_id=%s "
                "ORDER BY t.lifecycle_revision,t.id"
            ),
            (run_id,),
        ),
        "terminal_decisions": (
            (
                "SELECT row_to_json(d) FROM terminal_decisions d WHERE d.run_id=%s "
                "ORDER BY d.created_at,d.id"
            ),
            (run_id,),
        ),
        "run_mode_history": (
            """SELECT row_to_json(e) FROM research_events e WHERE e.run_id=%s AND (
               e.event_type::text='run.execution_mode_changed'
               OR e.payload->>'event'='run.execution_mode_changed'
               OR e.payload->>'event_type'='run.execution_mode_changed')
               ORDER BY e.created_at,e.id""",
            (run_id,),
        ),
        "promotion_subjects": (
            (
                "SELECT row_to_json(s) FROM run_asset_promotion_subjects s WHERE s.run_id=%s "
                "ORDER BY s.created_at,s.id"
            ),
            (run_id,),
        ),
        "promotion_events": (
            (
                "SELECT row_to_json(e) FROM run_asset_promotion_events e WHERE e.run_id=%s "
                "ORDER BY e.occurred_at,e.id"
            ),
            (run_id,),
        ),
        "indexing_checkpoint_observations": (
            """SELECT row_to_json(o) FROM indexing_checkpoint_observations o
               WHERE o.checkpoint_id IN (SELECT id FROM indexing_checkpoints WHERE run_id=%s)
               ORDER BY o.observed_at,o.id""",
            (run_id,),
        ),
        "invocations": (
            (
                "SELECT row_to_json(i) FROM research_invocations i WHERE i.run_id=%s "
                "ORDER BY i.created_at,i.id"
            ),
            (run_id,),
        ),
        "search_plans": (
            "SELECT row_to_json(p) FROM search_plans p WHERE p.run_id=%s ORDER BY p.created_at,p.id",
            (run_id,),
        ),
        "search_plan_queries": (
            (
                "SELECT row_to_json(q) FROM search_plan_queries q WHERE q.run_id=%s "
                "ORDER BY q.created_at,q.id"
            ),
            (run_id,),
        ),
        "search_responses": (
            (
                "SELECT row_to_json(r) FROM search_responses r WHERE r.run_id=%s "
                "ORDER BY r.created_at,r.id"
            ),
            (run_id,),
        ),
        "search_candidates": (
            (
                "SELECT row_to_json(c) FROM search_candidates c WHERE c.run_id=%s "
                "ORDER BY c.created_at,c.id"
            ),
            (run_id,),
        ),
        "candidate_occurrences": (
            (
                "SELECT row_to_json(o) FROM candidate_occurrences o WHERE o.run_id=%s "
                "ORDER BY o.discovered_at,o.id"
            ),
            (run_id,),
        ),
        "retrieval_events": (
            (
                "SELECT row_to_json(e) FROM retrieval_events e WHERE e.run_id=%s "
                "ORDER BY e.created_at,e.id"
            ),
            (run_id,),
        ),
        "run_assets": (
            (
                "SELECT row_to_json(a) FROM research_run_assets a WHERE a.run_id=%s "
                "ORDER BY a.snapshot_id,a.role"
            ),
            (run_id,),
        ),
        "sources": (
            """SELECT row_to_json(s) FROM sources s WHERE s.id IN (
               SELECT snap.source_id FROM asset_snapshots snap
               JOIN research_run_assets a ON a.snapshot_id=snap.id WHERE a.run_id=%s)
               ORDER BY s.id""",
            (run_id,),
        ),
        "snapshots": (
            """SELECT row_to_json(s) FROM asset_snapshots s WHERE s.id IN (
               SELECT snapshot_id FROM research_run_assets WHERE run_id=%s)
               ORDER BY s.retrieved_at,s.id""",
            (run_id,),
        ),
        "documents": (
            """SELECT row_to_json(d) FROM documents d WHERE d.snapshot_id IN (
               SELECT snapshot_id FROM research_run_assets WHERE run_id=%s)
               ORDER BY d.snapshot_id,d.id""",
            (run_id,),
        ),
        "document_derivations": (
            """SELECT row_to_json(d) FROM document_derivations d WHERE d.snapshot_id IN (
               SELECT snapshot_id FROM research_run_assets WHERE run_id=%s)
               ORDER BY d.created_at,d.id""",
            (run_id,),
        ),
        "document_blocks": (
            """SELECT row_to_json(b) FROM document_blocks b WHERE b.document_id IN (
               SELECT d.id FROM documents d WHERE d.snapshot_id IN (
                 SELECT snapshot_id FROM research_run_assets WHERE run_id=%s))
               ORDER BY b.document_id,b.ordinal,b.id""",
            (run_id,),
        ),
        "chunks": (
            """SELECT row_to_json(c) FROM chunks c WHERE c.document_id IN (
               SELECT d.id FROM documents d WHERE d.snapshot_id IN (
                 SELECT snapshot_id FROM research_run_assets WHERE run_id=%s))
               ORDER BY c.document_id,c.ordinal,c.id""",
            (run_id,),
        ),
        "ingestion_batches": (
            (
                "SELECT row_to_json(b) FROM ingestion_batches b WHERE b.research_run_id=%s "
                "ORDER BY b.started_at,b.id"
            ),
            (run_id,),
        ),
        "ingestion_batch_assets": (
            """SELECT row_to_json(a) FROM ingestion_batch_assets a WHERE a.batch_id IN (
               SELECT id FROM ingestion_batches WHERE research_run_id=%s)
               ORDER BY a.batch_id,a.ordinal,a.id""",
            (run_id,),
        ),
        "semantic_calls": (
            "SELECT row_to_json(c) FROM semantic_calls c WHERE c.run_id=%s ORDER BY c.created_at,c.id",
            (run_id,),
        ),
        "semantic_artifacts": (
            (
                "SELECT row_to_json(a) FROM semantic_artifacts a WHERE a.run_id=%s "
                "ORDER BY a.created_at,a.id"
            ),
            (run_id,),
        ),
        "synthesis_stages": (
            (
                "SELECT row_to_json(s) FROM synthesis_stages s WHERE s.run_id=%s "
                "ORDER BY s.created_at,s.id"
            ),
            (run_id,),
        ),
        "evidence_packets": (
            (
                "SELECT row_to_json(p) FROM evidence_packets p WHERE p.run_id=%s "
                "ORDER BY p.packet_revision,p.id"
            ),
            (run_id,),
        ),
        "research_claims": (
            "SELECT row_to_json(c) FROM research_claims c WHERE c.run_id=%s ORDER BY c.created_at,c.id",
            (run_id,),
        ),
        "claim_evidence_links": (
            (
                "SELECT row_to_json(l) FROM claim_evidence_links l WHERE l.run_id=%s "
                "ORDER BY l.created_at,l.id"
            ),
            (run_id,),
        ),
        "blob_references": (
            """SELECT row_to_json(x) FROM (
               SELECT s.id AS snapshot_id,s.raw_blob_uri AS blob_uri,
                      s.content_sha256,s.raw_byte_length AS byte_length
               FROM asset_snapshots s WHERE s.id IN (
                 SELECT snapshot_id FROM research_run_assets WHERE run_id=%s)
               ORDER BY s.id) x""",
            (run_id,),
        ),
    }
    return {
        name: _section(cursor, sql, params) for name, (sql, params) in specs.items()
    }


def _connect(config: Any):
    config.require_database()
    from .postgres import connect

    return connect(config.database_url)


def _build_v2(
    config: Any,
    identifier: str,
    *,
    schema_version: str,
    artifact_kind: str,
) -> dict[str, Any]:
    with _connect(config) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        transaction = _transaction(cursor)
        if artifact_kind == "run-export":
            transaction.pop("observed_at")
        run = _resolve_run(cursor, identifier)
        run_id = UUID(str(run["id"]))
        database_schema = _schema_version(cursor)
        sections = _core_sections(cursor, run_id)
        membership, seal_ids = _membership(cursor, run_id)
        seal = membership.get("active_seal")
        seal_id = UUID(str(seal["id"])) if isinstance(seal, Mapping) else None
        checkpoint, checkpoint_ids, fingerprint = _checkpoint(cursor, run_id, seal_id)
        census_ids = seal_ids or checkpoint_ids
        index = _index_sections(
            connection,
            cursor,
            census_ids,
            fingerprint,
            config.max_index_attempts,
        )
        sections.update(
            {
                "index_jobs": index["index_jobs"],
                "embedding_manifests": index["embedding_manifests"],
                "active_leases": index["active_leases"],
                "relevant_worker_heartbeats": index["heartbeats"],
            }
        )
        cursor.execute(
            """SELECT count(*) FROM search_responses WHERE run_id=%s AND provenance_status IN
               ('historical_unresolved','unresolved_compatibility')""",
            (run_id,),
        )
        unresolved_search = int(cursor.fetchone()[0])
        timing = _terminal_timing(cursor, run_id, census_ids, fingerprint)
        projection = _projection(cursor, census_ids, fingerprint)
        completion_counts = {
            name: sections[name]["exact_count"]
            for name in (
                "evidence_packets",
                "semantic_calls",
                "semantic_artifacts",
                "synthesis_stages",
                "research_claims",
                "claim_evidence_links",
            )
        }
        diagnostics = _diagnostics(
            run,
            membership,
            seal_ids,
            checkpoint,
            checkpoint_ids,
            index["census"],
            timing,
            unresolved_search,
            completion_counts,
        )
        return {
            "artifact_kind": artifact_kind,
            "schema_version": schema_version,
            "database_schema_version": database_schema,
            "snapshot_transaction": transaction,
            "run": _safe(run),
            "run_sha256": _sha(_safe(run)),
            "sections": sections,
            "exact_counts": {
                name: section["exact_count"] for name, section in sections.items()
            },
            "membership": membership,
            "indexing_checkpoint": checkpoint,
            "index_job_census": index["census"],
            "terminal_index_timing": timing,
            "qdrant_projection_reconciliation": projection,
            "diagnostics": diagnostics,
            "lifecycle_ledger_sha256": sections["lifecycle_ledger"]["sha256"],
            "blob_integrity_sha256": sections["blob_references"]["sha256"],
            "limits": {
                "section_item_limit": SECTION_ITEM_LIMIT,
                "nested_item_limit": NESTED_ITEM_LIMIT,
                "mapping_item_limit": MAPPING_ITEM_LIMIT,
                "text_character_limit": TEXT_CHARACTER_LIMIT,
            },
        }


def build_run_export(
    config: Any, identifier: str, schema_version: str
) -> dict[str, Any]:
    if schema_version not in EXPORT_RUN_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported export-run schema version: {schema_version}")
    if schema_version == "export-run-v1":
        with _connect(config) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            run = _resolve_run(cursor, identifier)
            run_id = UUID(str(run["id"]))
            cursor.execute(
                """SELECT row_to_json(e) FROM retrieval_events e
                   WHERE e.run_id=%s ORDER BY e.created_at,e.id""",
                (run_id,),
            )
            return {
                "schema_version": "export-run-v1",
                "run": _safe(run),
                "retrieval_events": [_safe(row[0]) for row in cursor.fetchall()],
            }
    return _build_v2(
        config,
        identifier,
        schema_version="export-run-v2",
        artifact_kind="run-export",
    )


def build_integrity_report(
    config: Any, identifier: str, schema_version: str
) -> dict[str, Any]:
    if schema_version not in INTEGRITY_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported integrity schema version: {schema_version}")
    return _build_v2(
        config,
        identifier,
        schema_version="integrity-v1",
        artifact_kind="run-integrity",
    )
