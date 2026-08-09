"""PostgreSQL-authoritative reconciliation of Qdrant projections.

Run reconciliation is read-only by default and is anchored to the persisted
completed indexing checkpoint plus its asset-membership seal.
"""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from .config import StoreConfig
from .postgres import connect
from .qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex
from .reconciliation_scope import (
    ReconciliationError,
    ReconciliationScope,
    _load_postgres_projection_state,
    _load_projection_scope,
    _load_run_scope,
)

_RETRIEVE_BATCH = 256


def _qdrant(config: StoreConfig, definition: dict[str, Any]) -> QdrantIndex:
    return QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        definition["physical_collection"],
        int(definition["dimension"]),
        definition["distance_metric"],
    )


def _chunks(values: list[str], size: int = _RETRIEVE_BATCH) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _scroll_point_ids(index: QdrantIndex) -> set[str]:
    point_ids: set[str] = set()
    offset = None
    while True:
        page = index.point_ids(offset, limit=_RETRIEVE_BATCH)
        point_ids.update(str(item["id"]) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            return point_ids


def _compare_payloads(
    index: QdrantIndex,
    expected_payloads: dict[str, dict[str, Any]],
    present_expected_ids: set[str],
) -> dict[str, Any]:
    ordered = sorted(present_expected_ids)
    retrieved_by_id: dict[str, dict] = {}
    batches = 0
    for batch in _chunks(ordered):
        batches += 1
        for point in index.retrieve(batch, with_payload=True):
            retrieved_by_id[str(point.get("id"))] = point
    missing_retrieval = sorted(set(ordered) - set(retrieved_by_id))
    mismatches: list[dict[str, Any]] = []
    mismatch_ids: set[str] = set()
    for point_id in ordered:
        point = retrieved_by_id.get(point_id)
        if point is None:
            continue
        expected = expected_payloads.get(point_id)
        if expected is None:
            mismatches.append(
                {
                    "point_id": point_id,
                    "field": "_postgres",
                    "reason": "missing_expected_record",
                }
            )
            mismatch_ids.add(point_id)
            continue
        payload = point.get("payload") or {}
        for field in PAYLOAD_INDEX_SCHEMAS:
            expected_value = expected.get(field)
            actual_value = payload.get(field)
            if actual_value is not None:
                actual_value = str(actual_value)
            if expected_value is not None:
                expected_value = str(expected_value)
            if actual_value != expected_value:
                mismatches.append(
                    {
                        "point_id": point_id,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
                mismatch_ids.add(point_id)
    return {
        "complete": not missing_retrieval,
        "expected": len(ordered),
        "retrieved": len(retrieved_by_id),
        "batches": batches,
        "missing_retrieval_ids": missing_retrieval,
        "mismatch_count": len(mismatches),
        "mismatched_point_count": len(mismatch_ids),
        "mismatch_point_ids": sorted(mismatch_ids),
        "mismatches": mismatches[:100],
        "mismatches_truncated": len(mismatches) > 100,
    }


def _repair_postgres_jobs(
    config: StoreConfig,
    scope: ReconciliationScope,
    chunk_ids: set[str],
) -> dict[str, int]:
    if not chunk_ids:
        return {"manifests_created_or_reused": 0, "jobs_requeued": 0}
    definition = scope.definition
    definition_id = UUID(definition["id"])
    ids = [UUID(value) for value in sorted(chunk_ids)]
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO embedding_manifests(
                   chunk_id,model_name,model_revision,dimension,distance_metric,
                   normalization,instruction_template_hash,qdrant_collection,
                   qdrant_point_id,index_status,index_definition_id)
                 SELECT c.id,%s,%s,%s,%s,%s,%s,%s,c.id,'pending',%s
                   FROM chunks c WHERE c.id=ANY(%s)
                 ON CONFLICT(chunk_id,index_definition_id) DO UPDATE SET
                   qdrant_collection=excluded.qdrant_collection
                 RETURNING id""",
            (
                definition["model_name"],
                definition["model_revision"],
                definition["dimension"],
                definition["distance_metric"],
                definition["normalization"],
                definition["instruction_template_hash"],
                definition["physical_collection"],
                definition_id,
                ids,
            ),
        )
        manifest_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """INSERT INTO index_jobs(
                   entity_type,entity_id,index_name,operation,status,
                   manifest_id,index_definition_id)
                 SELECT 'chunk',m.chunk_id,%s,'upsert','pending',m.id,%s
                   FROM embedding_manifests m
                  WHERE m.index_definition_id=%s AND m.chunk_id=ANY(%s)
                 ON CONFLICT(manifest_id,operation) DO NOTHING""",
            (
                definition["physical_collection"],
                definition_id,
                definition_id,
                ids,
            ),
        )
        cursor.execute(
            """UPDATE index_jobs j SET status='pending',available_at=now(),
                   attempt_count=0,started_at=NULL,completed_at=NULL,error=NULL,
                   lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,
                   updated_at=now()
                 FROM embedding_manifests m
                WHERE j.manifest_id=m.id AND j.operation='upsert'
                  AND m.index_definition_id=%s AND m.chunk_id=ANY(%s)""",
            (definition_id, ids),
        )
        jobs_requeued = cursor.rowcount
        cursor.execute(
            """UPDATE embedding_manifests SET index_status='pending',
                   indexed_at=NULL,error=NULL
                WHERE index_definition_id=%s AND chunk_id=ANY(%s)""",
            (definition_id, ids),
        )
    return {
        "manifests_created_or_reused": len(manifest_ids),
        "jobs_requeued": int(jobs_requeued),
    }


def _reconcile_scope(
    config: StoreConfig,
    scope: ReconciliationScope,
    *,
    repair: bool,
) -> dict[str, Any]:
    pg = _load_postgres_projection_state(config, scope)
    expected_ids = {str(value) for value in scope.expected_ids}
    manifest_ids = set(pg["manifests"])
    complete_manifests = {
        chunk_id
        for chunk_id, manifest in pg["manifests"].items()
        if manifest["status"] == "complete"
    }
    complete_jobs = {
        chunk_id
        for chunk_id, statuses in pg["jobs"].items()
        if "complete" in statuses
    }
    index = _qdrant(config, scope.definition)
    aliases = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.qdrant_alias,
        int(scope.definition["dimension"]),
        scope.definition["distance_metric"],
    ).list_aliases()
    expected_collection = scope.definition["physical_collection"]
    alias_target = aliases.get(config.qdrant_alias)
    schema = index.inspect_schema()
    point_ids = _scroll_point_ids(index) if schema.get("exists") else set()
    missing_points = expected_ids - point_ids
    orphaned_definition_points = point_ids - set(pg["definition_ids"])
    present_expected = expected_ids & point_ids
    payload_scan = _compare_payloads(index, pg["payloads"], present_expected)
    payload_index_status = index.inspect_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
    shard_health = index.inspect_shard_health()

    discrepancies: list[str] = []
    missing_manifests = expected_ids - manifest_ids
    incomplete_manifests = expected_ids - complete_manifests
    incomplete_jobs = expected_ids - complete_jobs
    if missing_manifests:
        discrepancies.append(f"{len(missing_manifests)} sealed chunks have no manifest")
    if incomplete_manifests:
        discrepancies.append(
            f"{len(incomplete_manifests)} sealed chunks lack complete manifests"
        )
    if incomplete_jobs:
        discrepancies.append(
            f"{len(incomplete_jobs)} sealed chunks lack complete index jobs"
        )
    if not schema.get("exists") or not schema.get("compatible"):
        discrepancies.append("Qdrant vector schema is missing or incompatible")
    if scope.scope == "run" or scope.definition.get("lifecycle_status") == "active":
        if alias_target != expected_collection:
            discrepancies.append(
                f"alias {config.qdrant_alias} targets {alias_target!r}, "
                f"expected {expected_collection!r}"
            )
    if missing_points:
        discrepancies.append(f"{len(missing_points)} sealed Qdrant points are missing")
    if orphaned_definition_points:
        discrepancies.append(
            f"{len(orphaned_definition_points)} Qdrant points are orphaned from "
            "PostgreSQL definition membership"
        )
    if not payload_scan["complete"]:
        discrepancies.append(
            "payload scan did not retrieve every present expected point"
        )
    if payload_scan["mismatch_count"]:
        discrepancies.append(
            f"{payload_scan['mismatch_count']} payload identity field "
            "mismatches detected"
        )
    missing_payload_indexes = sorted(
        field for field, detail in payload_index_status.items() if not detail["present"]
    )
    incompatible_payload_indexes = sorted(
        field
        for field, detail in payload_index_status.items()
        if detail["present"] and not detail["compatible"]
    )
    if missing_payload_indexes:
        discrepancies.append(
            "missing payload indexes: " + ", ".join(missing_payload_indexes)
        )
    if incompatible_payload_indexes:
        discrepancies.append(
            "incompatible payload indexes: " + ", ".join(incompatible_payload_indexes)
        )
    if not shard_health["healthy"]:
        discrepancies.append("Qdrant shard topology is not fully active and stable")

    repair_actions: list[dict[str, Any]] = []
    repair_blockers: list[str] = []
    if repair:
        requeue = (
            missing_points
            | set(payload_scan["mismatch_point_ids"])
            | missing_manifests
            | incomplete_manifests
            | incomplete_jobs
        )
        if requeue:
            repair_actions.append(
                {
                    "action": "requeue_exact_chunks",
                    **_repair_postgres_jobs(config, scope, requeue),
                }
            )
        if orphaned_definition_points:
            index.delete(sorted(orphaned_definition_points))
            repair_actions.append(
                {
                    "action": "delete_orphaned_qdrant_points",
                    "count": len(orphaned_definition_points),
                }
            )
        if missing_payload_indexes:
            compatibility = index.ensure_payload_indexes(
                {
                    field: PAYLOAD_INDEX_SCHEMAS[field]
                    for field in missing_payload_indexes
                },
                create_missing=True,
            )
            repair_actions.append(
                {"action": "create_missing_payload_indexes", "result": compatibility}
            )
        if incompatible_payload_indexes:
            repair_blockers.append(
                "incompatible payload-index schemas require an explicit schema "
                "migration; not overwritten"
            )
        if not schema.get("exists") or not schema.get("compatible"):
            repair_blockers.append(
                "vector-schema repair is destructive; rebuild/activate the "
                "projection explicitly"
            )
        if alias_target != expected_collection:
            repair_blockers.append(
                "alias mismatch must be corrected through index-activate/rollback "
                "so lifecycle journaling is preserved"
            )
        if not shard_health["healthy"]:
            repair_blockers.append(
                "shard health requires Qdrant cluster remediation; reconciliation "
                "will not rewrite shard topology"
            )

    result: dict[str, Any] = {
        "schema_version": "qdrant-reconciliation-v2",
        "scope": scope.scope,
        "read_only": not repair,
        "run_id": str(scope.run_id) if scope.run_id else None,
        "external_run_id": scope.external_run_id,
        "checkpoint": (
            {
                "id": str(scope.checkpoint_id),
                "membership_sha256": scope.membership_sha256,
                "expected": len(scope.expected_ids),
                "fingerprint": scope.fingerprint,
            }
            if scope.checkpoint_id
            else None
        ),
        "asset_membership": (
            {
                "seal_id": str(scope.seal_id),
                "seal_status": scope.seal_status,
                "membership_sha256": scope.asset_membership_sha256,
                "expected_chunks": len(scope.expected_ids),
            }
            if scope.seal_id
            else None
        ),
        "index_definition": scope.definition,
        "postgres": {
            "expected": len(expected_ids),
            "manifests": len(manifest_ids),
            "complete_manifests": len(complete_manifests),
            "complete_jobs": len(complete_jobs),
            "missing_manifest_ids": sorted(missing_manifests),
            "incomplete_manifest_ids": sorted(incomplete_manifests),
            "incomplete_job_ids": sorted(incomplete_jobs),
            "definition_membership": len(pg["definition_ids"]),
        },
        "qdrant": {
            "collection": expected_collection,
            "alias": config.qdrant_alias,
            "alias_target": alias_target,
            "alias_matches": alias_target == expected_collection,
            "schema": schema,
            "point_count": len(point_ids),
            "run_coverage": {
                "expected": len(expected_ids),
                "present": len(present_expected),
                "missing": len(missing_points),
                "missing_ids": sorted(missing_points),
            },
            "definition_coverage": {
                "postgres_members": len(pg["definition_ids"]),
                "qdrant_points": len(point_ids),
                "orphaned": len(orphaned_definition_points),
                "orphaned_ids": sorted(orphaned_definition_points),
            },
            "payload_scan": payload_scan,
            "payload_indexes": payload_index_status,
            "shard_state": shard_health["shards"],
            "shard_health": shard_health,
        },
        "discrepancies": discrepancies,
        "ok": not discrepancies,
        "repair_actions": repair_actions,
        "repair_blockers": repair_blockers,
    }
    if repair:
        result["post_repair"] = _reconcile_scope(config, scope, repair=False)
    return result


def reconcile_run(
    config: StoreConfig, identifier: str | UUID, *, repair: bool = False
) -> dict[str, Any]:
    """Reconcile one run from its immutable completed checkpoint/seal."""
    config.require_database()
    return _reconcile_scope(config, _load_run_scope(config, identifier), repair=repair)


def reconcile_projection(
    config: StoreConfig, *, repair: bool = False
) -> dict[str, Any]:
    """Projection-wide compatibility health, explicitly not run provenance."""
    config.require_database()
    try:
        scope = _load_projection_scope(config)
    except ReconciliationError as exc:
        return {
            "schema_version": "qdrant-reconciliation-v2",
            "scope": "projection",
            "read_only": not repair,
            "authoritative_membership": False,
            "total_active_chunks": 0,
            "definitions": [],
            "manifests": {},
            "jobs": {},
            "qdrant": {"ok": False, "aliases": {}, "collections": {}},
            "discrepancies": [str(exc)],
            "ok": False,
            "repair_actions": [],
            "repair_blockers": [str(exc)] if repair else [],
            "repaired": [],
        }
    result = _reconcile_scope(config, scope, repair=repair)
    result["total_active_chunks"] = result["postgres"]["expected"]
    result["definitions"] = [scope.definition]
    result["manifests"] = {
        scope.definition["id"]: {
            "complete": result["postgres"]["complete_manifests"],
            "total": result["postgres"]["manifests"],
        }
    }
    result["jobs"] = {
        scope.definition["id"]: {"complete": result["postgres"]["complete_jobs"]}
    }
    collection = scope.definition["physical_collection"]
    qdrant = result["qdrant"]
    result["qdrant"] = {
        "ok": result["ok"],
        "aliases": {qdrant["alias"]: qdrant["alias_target"]}
        if qdrant["alias_target"]
        else {},
        "collections": {
            collection: {
                "alias": qdrant["alias"] if qdrant["alias_matches"] else None,
                "schema": qdrant["schema"],
                "point_count": qdrant["point_count"],
                "cached_point_count": None,
                "coverage": {
                    "missing": qdrant["run_coverage"]["missing"],
                    "orphaned": qdrant["definition_coverage"]["orphaned"],
                },
                "payload_mismatches": qdrant["payload_scan"]["mismatches"],
                "payload_indexes": {
                    field: detail["compatible"]
                    for field, detail in qdrant["payload_indexes"].items()
                },
                "payload_index_details": qdrant["payload_indexes"],
                "shard_state": qdrant["shard_state"],
                "shard_health": qdrant["shard_health"],
                "has_discrepancies": not result["ok"],
            }
        },
    }
    result["repaired"] = [scope.definition["id"]] if result["repair_actions"] else []
    return result
