"""Bounded, secret-safe, PostgreSQL-snapshot run audit exports.

Issue #221 / ARC-15. PostgreSQL is the authority for lifecycle, provenance,
sealed completion membership, and durable indexing state. Qdrant evidence in
these exports is explicitly projection-only and never authorizes completion.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from .index_census import CENSUS_CLASSES, census_index_jobs
from .service import json_default

EXPORT_RUN_SCHEMA_VERSIONS = ("export-run-v1", "export-run-v2")
INTEGRITY_SCHEMA_VERSIONS = ("integrity-v1",)
SECTION_ITEM_LIMIT = 50
NESTED_ITEM_LIMIT = 25
TEXT_CHARACTER_LIMIT = 2_000
_REDACTED = "***REDACTED***"

_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"authorization|password|secret|credential|awsaccesskeyid|"
    r"x-amz-credential|signature|x-amz-signature|token)$",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"authorization|password|secret|credential|awsaccesskeyid|"
    r"x-amz-credential|signature|x-amz-signature|token)\s*[=:]\s*)"
    r"([^\s&,;]+)"
)
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"password|secret|credential|awsaccesskeyid|x-amz-credential|signature|"
    r"x-amz-signature|token)=)([^&#\s]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_URI_USERINFO = re.compile(r"(://[^/@:\s]+:)[^/@\s]+@")
_UNIX_HOME = re.compile(r"/(?:home|Users)/[^/\s]+")
_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=json_default,
    )


def _stable_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _redact_text(value: str) -> str:
    redacted = _SECRET_QUERY.sub(lambda match: match.group(1) + _REDACTED, value)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: match.group(1) + _REDACTED, redacted
    )
    redacted = _BEARER.sub(lambda match: f"{match.group(1)} {_REDACTED}", redacted)
    redacted = _URI_USERINFO.sub(lambda match: match.group(1) + _REDACTED + "@", redacted)
    redacted = _UNIX_HOME.sub("/***REDACTED_HOME***", redacted)
    redacted = _WINDOWS_HOME.sub(r"C:\\***REDACTED_HOME***", redacted)
    return redacted


def _safe_value(value: Any) -> Any:
    """Recursively redact secrets and bound nested raw content."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = (
                _REDACTED if _SECRET_KEY.fullmatch(key_text.strip()) else _safe_value(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        digest = hashlib.sha256()
        items: list[Any] = []
        for index, item in enumerate(value):
            safe = _safe_value(item)
            encoded = _canonical_json(safe).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            if index < NESTED_ITEM_LIMIT:
                items.append(safe)
        if len(value) <= NESTED_ITEM_LIMIT:
            return items
        return {
            "schema_version": "bounded-list-v1",
            "exact_count": len(value),
            "items": items,
            "items_limit": NESTED_ITEM_LIMIT,
            "sha256": digest.hexdigest(),
            "truncated": True,
        }
    if isinstance(value, str):
        redacted = _redact_text(value)
        if len(redacted) <= TEXT_CHARACTER_LIMIT:
            return redacted
        return {
            "schema_version": "bounded-text-v1",
            "character_count": len(redacted),
            "prefix": redacted[:TEXT_CHARACTER_LIMIT],
            "prefix_character_limit": TEXT_CHARACTER_LIMIT,
            "sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
            "truncated": True,
        }
    return value


def _empty_section() -> dict[str, Any]:
    return {
        "exact_count": 0,
        "items": [],
        "items_limit": SECTION_ITEM_LIMIT,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "truncated": False,
    }


def _bounded_section(
    cursor: Any,
    sql: str,
    params: Sequence[Any] = (),
    *,
    item_limit: int = SECTION_ITEM_LIMIT,
) -> dict[str, Any]:
    """Read an exactly counted row stream but retain only bounded safe items."""
    cursor.execute(sql, tuple(params))
    digest = hashlib.sha256()
    items: list[Any] = []
    exact_count = 0
    for row in cursor:
        safe = _safe_value(row[0])
        encoded = _canonical_json(safe).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        exact_count += 1
        if len(items) < item_limit:
            items.append(safe)
    return {
        "exact_count": exact_count,
        "items": items,
        "items_limit": item_limit,
        "sha256": digest.hexdigest(),
        "truncated": exact_count > item_limit,
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
        cursor.execute("SELECT row_to_json(r) FROM research_runs r WHERE id=%s", (run_id,))
    row = cursor.fetchone()
    if row is None:
        raise SystemExit("research run not found")
    return row[0]


def _database_schema_version(cursor: Any) -> str | None:
    cursor.execute("SELECT version_num FROM alembic_version")
    row = cursor.fetchone()
    return None if row is None else str(row[0])


def _transaction_metadata(cursor: Any) -> dict[str, Any]:
    cursor.execute(
        "SELECT transaction_timestamp(), current_setting('transaction_isolation'), "
        "current_setting('transaction_read_only')"
    )
    observed_at, isolation, read_only = cursor.fetchone()
    return {
        "observed_at": observed_at,
        "isolation": str(isolation),
        "read_only": str(read_only).lower() == "on",
    }


def _membership_context(cursor: Any, run_id: UUID) -> tuple[dict[str, Any], list[UUID]]:
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
            "members": _empty_section(),
            "exact_chunk_count": 0,
            "exact_chunk_ids_sha256": _stable_sha256([]),
        }, []

    raw_seal = row[0]
    seal_id = UUID(str(raw_seal["id"]))
    cursor.execute(
        """SELECT row_to_json(m) FROM run_asset_membership_members m
             WHERE m.seal_id=%s ORDER BY m.ordinal,m.subject_id""",
        (seal_id,),
    )
    digest = hashlib.sha256()
    items: list[Any] = []
    exact_count = 0
    chunk_ids: set[UUID] = set()
    for member_row in cursor:
        raw_member = member_row[0]
        for chunk_id in raw_member.get("chunk_ids") or ():
            chunk_ids.add(UUID(str(chunk_id)))
        safe = _safe_value(raw_member)
        encoded = _canonical_json(safe).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        exact_count += 1
        if len(items) < SECTION_ITEM_LIMIT:
            items.append(safe)
    ordered_chunk_ids = sorted(chunk_ids, key=str)
    members = {
        "exact_count": exact_count,
        "items": items,
        "items_limit": SECTION_ITEM_LIMIT,
        "sha256": digest.hexdigest(),
        "truncated": exact_count > SECTION_ITEM_LIMIT,
    }
    return {
        "active_seal": _safe_value(raw_seal),
        "members": members,
        "exact_chunk_count": len(ordered_chunk_ids),
        "exact_chunk_ids_sha256": _stable_sha256([str(item) for item in ordered_chunk_ids]),
    }, ordered_chunk_ids


def _latest_checkpoint(
    cursor: Any, run_id: UUID, seal_id: UUID | None
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
    raw = row[0]
    entity_ids = [UUID(str(item)) for item in raw.get("entity_ids") or ()]
    fingerprint = raw.get("fingerprint")
    return _safe_value(raw), sorted(set(entity_ids), key=str), (
        str(fingerprint) if fingerprint else None
    )


def _exact_index_sections(
    connection: Any,
    cursor: Any,
    entity_ids: Sequence[UUID],
    fingerprint: str | None,
    *,
    max_attempts: int,
) -> dict[str, Any]:
    if not entity_ids:
        return {
            "census": None,
            "index_jobs": _empty_section(),
            "embedding_manifests": _empty_section(),
            "active_leases": _empty_section(),
            "heartbeats": _empty_section(),
        }

    index_jobs = _bounded_section(
        cursor,
        """SELECT row_to_json(j) FROM index_jobs j
             WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
             ORDER BY j.entity_id,j.id""",
        (list(entity_ids),),
    )
    manifests = _bounded_section(
        cursor,
        """SELECT row_to_json(m) FROM embedding_manifests m
             WHERE m.chunk_id=ANY(%s::uuid[]) ORDER BY m.chunk_id,m.id""",
        (list(entity_ids),),
    )
    active_leases = _bounded_section(
        cursor,
        """SELECT row_to_json(x) FROM (
               SELECT j.id AS job_id,j.entity_id,j.manifest_id,j.index_definition_id,
                      j.status,j.attempt_count,j.lease_owner,j.lease_expires_at
                 FROM index_jobs j
                WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
                  AND j.lease_token IS NOT NULL
                ORDER BY j.entity_id,j.id
             ) x""",
        (list(entity_ids),),
    )
    heartbeats = _bounded_section(
        cursor,
        """SELECT row_to_json(h) FROM index_worker_heartbeats h
             WHERE h.worker_id IN (
               SELECT DISTINCT j.lease_owner FROM index_jobs j
                WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
                  AND j.lease_owner IS NOT NULL
             ) ORDER BY h.worker_id""",
        (list(entity_ids),),
    )
    census = None
    if fingerprint:
        census = _safe_value(
            census_index_jobs(
                connection,
                list(entity_ids),
                fingerprint,
                max_attempts=max_attempts,
                representative_limit=SECTION_ITEM_LIMIT,
            )
        )
    return {
        "census": census,
        "index_jobs": index_jobs,
        "embedding_manifests": manifests,
        "active_leases": active_leases,
        "heartbeats": heartbeats,
    }


def _late_completion_evidence(
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
            "completed_after_decision": _empty_section(),
            "spanning_terminal_decision_exact_count": 0,
            "historical_identity_correlation": {
                "status": "inconclusive",
                "reason_code": "no_terminal_decision",
            },
        }
    raw_decision = row[0]
    decision_at = raw_decision.get("created_at")
    if not entity_ids or decision_at is None:
        return {
            "terminal_decision": _safe_value(raw_decision),
            "completed_after_decision": _empty_section(),
            "spanning_terminal_decision_exact_count": 0,
            "historical_identity_correlation": {
                "status": "inconclusive",
                "reason_code": "no_exact_membership_for_terminal_correlation",
            },
        }

    fingerprint_clause = ""
    params: list[Any] = [list(entity_ids), decision_at]
    if fingerprint:
        fingerprint_clause = " AND d.fingerprint=%s"
        params.append(fingerprint)
    completed = _bounded_section(
        cursor,
        """SELECT row_to_json(x) FROM (
               SELECT j.id AS job_id,j.entity_id,j.manifest_id,j.index_definition_id,
                      j.status,j.attempt_count,j.started_at,j.completed_at,
                      (j.started_at IS NOT NULL AND j.started_at <= %s
                       AND j.completed_at > %s) AS spans_terminal_decision
                 FROM index_jobs j
                 JOIN embedding_manifests m ON m.id=j.manifest_id
                 JOIN index_definitions d ON d.id=m.index_definition_id
                WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
                  AND j.completed_at > %s"""
        + fingerprint_clause
        + " ORDER BY j.completed_at,j.entity_id,j.id) x",
        tuple([decision_at, decision_at] + params),
    )
    count_params: list[Any] = [list(entity_ids), decision_at, decision_at]
    if fingerprint:
        count_params.append(fingerprint)
    cursor.execute(
        """SELECT count(*) FROM index_jobs j
             JOIN embedding_manifests m ON m.id=j.manifest_id
             JOIN index_definitions d ON d.id=m.index_definition_id
            WHERE j.entity_type='chunk' AND j.entity_id=ANY(%s::uuid[])
              AND j.started_at IS NOT NULL AND j.started_at <= %s
              AND j.completed_at > %s"""
        + (" AND d.fingerprint=%s" if fingerprint else ""),
        tuple(count_params),
    )
    spanning = int(cursor.fetchone()[0])
    terminal_census = raw_decision.get("state_census")
    running_live = None
    if isinstance(terminal_census, Mapping):
        counts = terminal_census.get("counts")
        if isinstance(counts, Mapping):
            running_live = counts.get("running_live")
        elif isinstance(terminal_census.get("running_live"), int):
            running_live = terminal_census.get("running_live")
    return {
        "terminal_decision": _safe_value(raw_decision),
        "completed_after_decision": completed,
        "spanning_terminal_decision_exact_count": spanning,
        "terminal_census_running_live_count": running_live,
        "historical_identity_correlation": {
            "status": "inconclusive",
            "reason_code": "terminal_census_does_not_persist_full_running_live_id_set",
            "note": (
                "The artifact preserves the persisted terminal census and exact later "
                "job timing separately; it does not invent per-job historical lease provenance."
            ),
        },
    }


def _projection_reconciliation(
    cursor: Any, entity_ids: Sequence[UUID], fingerprint: str | None
) -> dict[str, Any]:
    if not fingerprint:
        return {
            "status": "inconclusive",
            "reason_code": "no_persisted_index_fingerprint",
            "authoritative_for_completion": False,
            "definitions": _empty_section(),
            "point_count_cache": _empty_section(),
        }
    if not entity_ids:
        return {
            "status": "inconclusive",
            "reason_code": "no_exact_membership_for_projection_reconciliation",
            "authoritative_for_completion": False,
            "definitions": _empty_section(),
            "point_count_cache": _empty_section(),
        }
    definitions = _bounded_section(
        cursor,
        """SELECT row_to_json(d) FROM index_definitions d
             WHERE d.fingerprint=%s ORDER BY d.created_at,d.id""",
        (fingerprint,),
    )
    point_counts = _bounded_section(
        cursor,
        """SELECT row_to_json(p) FROM index_point_counts p
             WHERE p.index_definition_id IN (
               SELECT id FROM index_definitions WHERE fingerprint=%s
             ) ORDER BY p.last_verified_at,p.index_definition_id""",
        (fingerprint,),
    )
    cursor.execute(
        """SELECT count(*) FILTER (WHERE m.index_status='complete'),count(*)
             FROM embedding_manifests m
             JOIN index_definitions d ON d.id=m.index_definition_id
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
        "note": (
            "PostgreSQL manifest/job state is authoritative. Cached point counts are "
            "diagnostic only; a live Qdrant observation is intentionally not treated as "
            "lifecycle or exact-membership authority."
        ),
    }


def _diagnostics(
    *,
    run: Mapping[str, Any],
    membership: Mapping[str, Any],
    seal_ids: Sequence[UUID],
    checkpoint: Mapping[str, Any] | None,
    checkpoint_ids: Sequence[UUID],
    census: Mapping[str, Any] | None,
    terminal_timing: Mapping[str, Any],
    search_unresolved: int,
    completion_evidence: Mapping[str, int],
) -> dict[str, Any]:
    domains: dict[str, dict[str, Any]] = {}
    seal = membership.get("active_seal")
    if seal is None:
        if checkpoint is None:
            domains["membership"] = {
                "status": "inconclusive",
                "reason_code": "no_sealed_membership_or_checkpoint",
            }
        else:
            domains["membership"] = {
                "status": "inconclusive",
                "reason_code": "legacy_checkpoint_without_asset_membership_seal",
            }
    else:
        expected_assets = int(seal.get("expected_asset_count", -1))
        expected_chunks = int(seal.get("expected_chunk_count", -1))
        member_count = int(membership["members"]["exact_count"])
        actual_chunks = int(membership["exact_chunk_count"])
        mismatch = expected_assets != member_count or expected_chunks != actual_chunks
        if checkpoint is not None and checkpoint_ids:
            mismatch = mismatch or set(checkpoint_ids) != set(seal_ids)
        domains["membership"] = {
            "status": "failure" if mismatch else "pass",
            "reason_code": (
                "sealed_membership_checkpoint_or_count_mismatch"
                if mismatch
                else "sealed_membership_consistent"
            ),
            "expected_asset_count": expected_assets,
            "observed_asset_count": member_count,
            "expected_chunk_count": expected_chunks,
            "observed_chunk_count": actual_chunks,
        }

    if census is None:
        domains["indexing"] = {
            "status": "inconclusive",
            "reason_code": "exact_index_census_unavailable",
        }
    else:
        counts = census.get("counts") if isinstance(census.get("counts"), Mapping) else census
        noncomplete = {
            name: int(counts.get(name, 0))
            for name in CENSUS_CLASSES
            if name != "complete" and int(counts.get(name, 0)) > 0
        }
        domains["indexing"] = {
            "status": "failure" if noncomplete else "pass",
            "reason_code": "index_membership_incomplete" if noncomplete else "index_membership_complete",
            "noncomplete_classes": noncomplete,
            "decision_evidence": census.get("decision_evidence"),
        }

    terminal = terminal_timing.get("terminal_decision")
    if terminal is None:
        domains["terminal_decision"] = {
            "status": "inconclusive",
            "reason_code": "no_terminal_decision",
        }
    else:
        terminal_census = terminal.get("state_census")
        if not isinstance(terminal_census, Mapping) or terminal_census.get("available") is False:
            domains["terminal_decision"] = {
                "status": "inconclusive",
                "reason_code": (
                    terminal_census.get("reason", "terminal_census_unavailable")
                    if isinstance(terminal_census, Mapping)
                    else "terminal_census_missing"
                ),
            }
        else:
            counts = terminal_census.get("counts")
            if not isinstance(counts, Mapping):
                counts = terminal_census
            noncomplete = {
                name: int(counts.get(name, 0))
                for name in CENSUS_CLASSES
                if name != "complete" and isinstance(counts.get(name, 0), int) and int(counts.get(name, 0)) > 0
            }
            domains["terminal_decision"] = {
                "status": "failure" if noncomplete else "pass",
                "reason_code": (
                    "terminal_decision_census_contains_noncomplete_index_work"
                    if noncomplete
                    else "terminal_decision_census_complete"
                ),
                "noncomplete_classes": noncomplete,
                "later_completion_count": terminal_timing["completed_after_decision"]["exact_count"],
                "spanning_terminal_decision_exact_count": terminal_timing[
                    "spanning_terminal_decision_exact_count"
                ],
            }

    domains["search_provenance"] = {
        "status": "inconclusive" if search_unresolved else "pass",
        "reason_code": (
            "search_provenance_contains_explicitly_unresolved_history"
            if search_unresolved
            else "search_provenance_resolved_or_not_applicable"
        ),
        "unresolved_response_count": search_unresolved,
    }

    state = str(run.get("state") or "")
    if state == "completed":
        required = (
            "evidence_packets",
            "semantic_calls",
            "semantic_artifacts",
            "synthesis_stages",
        )
        missing = [name for name in required if completion_evidence.get(name, 0) <= 0]
        if missing:
            domains["synthesis_completion"] = {
                "status": "failure",
                "reason_code": "completed_run_missing_persisted_completion_provenance",
                "missing_evidence_domains": missing,
            }
        else:
            domains["synthesis_completion"] = {
                "status": "pass",
                "reason_code": "completed_run_has_persisted_completion_provenance",
                "note": (
                    "This diagnostic confirms required persisted provenance domains exist; "
                    "the production completion guard remains the authority for semantic validity."
                ),
            }
    else:
        domains["synthesis_completion"] = {
            "status": "inconclusive",
            "reason_code": "noncompleted_run_has_no_required_authoritative_completion",
        }

    statuses = {item["status"] for item in domains.values()}
    overall = "failure" if "failure" in statuses else (
        "inconclusive" if "inconclusive" in statuses else "pass"
    )
    return {"overall_status": overall, "domains": domains}


def _build_v2(config: Any, identifier: str, *, schema_version: str, artifact_kind: str) -> dict[str, Any]:
    with config_database(config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            transaction = _transaction_metadata(cursor)
            if artifact_kind == "run-export":
                transaction = {
                    "isolation": transaction["isolation"],
                    "read_only": transaction["read_only"],
                }
            run = _resolve_run(cursor, identifier)
            run_id = UUID(str(run["id"]))
            schema = _database_schema_version(cursor)

            lifecycle = _bounded_section(
                cursor,
                """SELECT row_to_json(t) FROM research_run_transitions t
                     WHERE t.run_id=%s ORDER BY t.lifecycle_revision,t.id""",
                (run_id,),
            )
            terminal_decisions = _bounded_section(
                cursor,
                """SELECT row_to_json(d) FROM terminal_decisions d
                     WHERE d.run_id=%s ORDER BY d.created_at,d.id""",
                (run_id,),
            )
            run_mode_history = _bounded_section(
                cursor,
                """SELECT row_to_json(e) FROM research_events e
                     WHERE e.run_id=%s AND (
                       e.event_type::text='run.execution_mode_changed'
                       OR e.payload->>'event'='run.execution_mode_changed'
                       OR e.payload->>'event_type'='run.execution_mode_changed'
                     ) ORDER BY e.created_at,e.id""",
                (run_id,),
            )
            promotion_subjects = _bounded_section(
                cursor,
                """SELECT row_to_json(s) FROM run_asset_promotion_subjects s
                     WHERE s.run_id=%s ORDER BY s.created_at,s.id""",
                (run_id,),
            )
            promotion_events = _bounded_section(
                cursor,
                """SELECT row_to_json(e) FROM run_asset_promotion_events e
                     WHERE e.run_id=%s ORDER BY e.occurred_at,e.id""",
                (run_id,),
            )
            checkpoint_observations = _bounded_section(
                cursor,
                """SELECT row_to_json(o) FROM indexing_checkpoint_observations o
                     WHERE o.checkpoint_id IN (
                       SELECT id FROM indexing_checkpoints WHERE run_id=%s
                     ) ORDER BY o.observed_at,o.id""",
                (run_id,),
            )
            membership, seal_ids = _membership_context(cursor, run_id)
            seal_id = None
            if isinstance(membership.get("active_seal"), Mapping):
                seal_id = UUID(str(membership["active_seal"]["id"]))
            checkpoint, checkpoint_ids, fingerprint = _latest_checkpoint(cursor, run_id, seal_id)
            census_ids = seal_ids or checkpoint_ids
            index = _exact_index_sections(
                connection,
                cursor,
                census_ids,
                fingerprint,
                max_attempts=config.max_index_attempts,
            )

            invocations = _bounded_section(
                cursor,
                """SELECT row_to_json(i) FROM research_invocations i
                     WHERE i.run_id=%s ORDER BY i.created_at,i.id""",
                (run_id,),
            )
            search_plans = _bounded_section(
                cursor,
                """SELECT row_to_json(p) FROM search_plans p
                     WHERE p.run_id=%s ORDER BY p.created_at,p.id""",
                (run_id,),
            )
            search_queries = _bounded_section(
                cursor,
                """SELECT row_to_json(q) FROM search_plan_queries q
                     WHERE q.run_id=%s ORDER BY q.created_at,q.id""",
                (run_id,),
            )
            search_responses = _bounded_section(
                cursor,
                """SELECT row_to_json(r) FROM search_responses r
                     WHERE r.run_id=%s ORDER BY r.created_at,r.id""",
                (run_id,),
            )
            cursor.execute(
                """SELECT count(*) FROM search_responses
                     WHERE run_id=%s AND provenance_status IN
                       ('historical_unresolved','unresolved_compatibility')""",
                (run_id,),
            )
            search_unresolved = int(cursor.fetchone()[0])
            candidates = _bounded_section(
                cursor,
                """SELECT row_to_json(c) FROM search_candidates c
                     WHERE c.run_id=%s ORDER BY c.created_at,c.id""",
                (run_id,),
            )
            candidate_occurrences = _bounded_section(
                cursor,
                """SELECT row_to_json(o) FROM candidate_occurrences o
                     WHERE o.run_id=%s ORDER BY o.discovered_at,o.id""",
                (run_id,),
            )
            retrieval_events = _bounded_section(
                cursor,
                """SELECT row_to_json(e) FROM retrieval_events e
                     WHERE e.run_id=%s ORDER BY e.created_at,e.id""",
                (run_id,),
            )
            run_assets = _bounded_section(
                cursor,
                """SELECT row_to_json(a) FROM research_run_assets a
                     WHERE a.run_id=%s ORDER BY a.snapshot_id,a.role""",
                (run_id,),
            )
            sources = _bounded_section(
                cursor,
                """SELECT row_to_json(s) FROM sources s
                     WHERE s.id IN (
                       SELECT snap.source_id FROM asset_snapshots snap
                       JOIN research_run_assets a ON a.snapshot_id=snap.id
                       WHERE a.run_id=%s
                     ) ORDER BY s.id""",
                (run_id,),
            )
            snapshots = _bounded_section(
                cursor,
                """SELECT row_to_json(s) FROM asset_snapshots s
                     WHERE s.id IN (
                       SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                     ) ORDER BY s.retrieved_at,s.id""",
                (run_id,),
            )
            documents = _bounded_section(
                cursor,
                """SELECT row_to_json(d) FROM documents d
                     WHERE d.snapshot_id IN (
                       SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                     ) ORDER BY d.snapshot_id,d.id""",
                (run_id,),
            )
            derivations = _bounded_section(
                cursor,
                """SELECT row_to_json(d) FROM document_derivations d
                     WHERE d.snapshot_id IN (
                       SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                     ) ORDER BY d.created_at,d.id""",
                (run_id,),
            )
            blocks = _bounded_section(
                cursor,
                """SELECT row_to_json(b) FROM document_blocks b
                     WHERE b.document_id IN (
                       SELECT d.id FROM documents d WHERE d.snapshot_id IN (
                         SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                       )
                     ) ORDER BY b.document_id,b.ordinal,b.id""",
                (run_id,),
            )
            chunks = _bounded_section(
                cursor,
                """SELECT row_to_json(c) FROM chunks c
                     WHERE c.document_id IN (
                       SELECT d.id FROM documents d WHERE d.snapshot_id IN (
                         SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                       )
                     ) ORDER BY c.document_id,c.ordinal,c.id""",
                (run_id,),
            )
            batches = _bounded_section(
                cursor,
                """SELECT row_to_json(b) FROM ingestion_batches b
                     WHERE b.research_run_id=%s ORDER BY b.started_at,b.id""",
                (run_id,),
            )
            batch_assets = _bounded_section(
                cursor,
                """SELECT row_to_json(a) FROM ingestion_batch_assets a
                     WHERE a.batch_id IN (
                       SELECT id FROM ingestion_batches WHERE research_run_id=%s
                     ) ORDER BY a.batch_id,a.ordinal,a.id""",
                (run_id,),
            )
            semantic_calls = _bounded_section(
                cursor,
                """SELECT row_to_json(c) FROM semantic_calls c
                     WHERE c.run_id=%s ORDER BY c.created_at,c.id""",
                (run_id,),
            )
            semantic_artifacts = _bounded_section(
                cursor,
                """SELECT row_to_json(a) FROM semantic_artifacts a
                     WHERE a.run_id=%s ORDER BY a.created_at,a.id""",
                (run_id,),
            )
            synthesis_stages = _bounded_section(
                cursor,
                """SELECT row_to_json(s) FROM synthesis_stages s
                     WHERE s.run_id=%s ORDER BY s.created_at,s.id""",
                (run_id,),
            )
            evidence_packets = _bounded_section(
                cursor,
                """SELECT row_to_json(p) FROM evidence_packets p
                     WHERE p.run_id=%s ORDER BY p.packet_revision,p.id""",
                (run_id,),
            )
            research_claims = _bounded_section(
                cursor,
                """SELECT row_to_json(c) FROM research_claims c
                     WHERE c.run_id=%s ORDER BY c.created_at,c.id""",
                (run_id,),
            )
            claim_evidence_links = _bounded_section(
                cursor,
                """SELECT row_to_json(l) FROM claim_evidence_links l
                     WHERE l.run_id=%s ORDER BY l.created_at,l.id""",
                (run_id,),
            )
            blob_references = _bounded_section(
                cursor,
                """SELECT row_to_json(x) FROM (
                       SELECT s.id AS snapshot_id,s.raw_blob_uri AS blob_uri,
                              s.content_sha256,s.raw_byte_length AS byte_length
                         FROM asset_snapshots s
                        WHERE s.id IN (
                          SELECT snapshot_id FROM research_run_assets WHERE run_id=%s
                        ) ORDER BY s.id
                     ) x""",
                (run_id,),
            )
            terminal_timing = _late_completion_evidence(
                cursor, run_id, census_ids, fingerprint
            )
            projection = _projection_reconciliation(cursor, census_ids, fingerprint)

            sections = {
                "lifecycle_ledger": lifecycle,
                "terminal_decisions": terminal_decisions,
                "run_mode_history": run_mode_history,
                "promotion_subjects": promotion_subjects,
                "promotion_events": promotion_events,
                "indexing_checkpoint_observations": checkpoint_observations,
                "invocations": invocations,
                "search_plans": search_plans,
                "search_plan_queries": search_queries,
                "search_responses": search_responses,
                "search_candidates": candidates,
                "candidate_occurrences": candidate_occurrences,
                "retrieval_events": retrieval_events,
                "run_assets": run_assets,
                "sources": sources,
                "snapshots": snapshots,
                "documents": documents,
                "document_derivations": derivations,
                "document_blocks": blocks,
                "chunks": chunks,
                "ingestion_batches": batches,
                "ingestion_batch_assets": batch_assets,
                "semantic_calls": semantic_calls,
                "semantic_artifacts": semantic_artifacts,
                "synthesis_stages": synthesis_stages,
                "evidence_packets": evidence_packets,
                "research_claims": research_claims,
                "claim_evidence_links": claim_evidence_links,
                "index_jobs": index["index_jobs"],
                "embedding_manifests": index["embedding_manifests"],
                "active_leases": index["active_leases"],
                "relevant_worker_heartbeats": index["heartbeats"],
                "blob_references": blob_references,
            }
            diagnostics = _diagnostics(
                run=run,
                membership=membership,
                seal_ids=seal_ids,
                checkpoint=checkpoint,
                checkpoint_ids=checkpoint_ids,
                census=index["census"],
                terminal_timing=terminal_timing,
                search_unresolved=search_unresolved,
                completion_evidence={
                    "evidence_packets": evidence_packets["exact_count"],
                    "semantic_calls": semantic_calls["exact_count"],
                    "semantic_artifacts": semantic_artifacts["exact_count"],
                    "synthesis_stages": synthesis_stages["exact_count"],
                    "research_claims": research_claims["exact_count"],
                    "claim_evidence_links": claim_evidence_links["exact_count"],
                },
            )
            exact_counts = {
                name: section["exact_count"] for name, section in sections.items()
            }
            result = {
                "artifact_kind": artifact_kind,
                "schema_version": schema_version,
                "database_schema_version": schema,
                "snapshot_transaction": transaction,
                "run": _safe_value(run),
                "run_sha256": _stable_sha256(_safe_value(run)),
                "sections": sections,
                "exact_counts": exact_counts,
                "membership": membership,
                "indexing_checkpoint": checkpoint,
                "index_job_census": index["census"],
                "terminal_index_timing": terminal_timing,
                "qdrant_projection_reconciliation": projection,
                "diagnostics": diagnostics,
                "lifecycle_ledger_sha256": lifecycle["sha256"],
                "blob_integrity_sha256": blob_references["sha256"],
                "limits": {
                    "section_item_limit": SECTION_ITEM_LIMIT,
                    "nested_item_limit": NESTED_ITEM_LIMIT,
                    "text_character_limit": TEXT_CHARACTER_LIMIT,
                },
            }
            return result


def config_database(config: Any):
    """Small seam kept local so tests can assert one connection/snapshot."""
    config.require_database()
    from .postgres import connect

    return connect(config.database_url)


def build_run_export(config: Any, identifier: str, schema_version: str) -> dict[str, Any]:
    if schema_version not in EXPORT_RUN_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported export-run schema version: {schema_version}")
    if schema_version == "export-run-v1":
        with config_database(config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                run = _resolve_run(cursor, identifier)
                run_id = UUID(str(run["id"]))
                cursor.execute(
                    """SELECT row_to_json(e) FROM retrieval_events e
                         WHERE e.run_id=%s ORDER BY e.created_at,e.id""",
                    (run_id,),
                )
                events = [_safe_value(row[0]) for row in cursor.fetchall()]
                return {
                    "schema_version": "export-run-v1",
                    "run": _safe_value(run),
                    "retrieval_events": events,
                }
    return _build_v2(
        config,
        identifier,
        schema_version="export-run-v2",
        artifact_kind="run-export",
    )


def build_integrity_report(config: Any, identifier: str, schema_version: str) -> dict[str, Any]:
    if schema_version not in INTEGRITY_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported integrity schema version: {schema_version}")
    return _build_v2(
        config,
        identifier,
        schema_version="integrity-v1",
        artifact_kind="run-integrity",
    )
