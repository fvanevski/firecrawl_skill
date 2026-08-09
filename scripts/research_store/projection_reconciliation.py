"""Operational projection reconciliation for legacy doctor/index tooling.

This module intentionally does *not* establish historical run provenance.  It
preserves the pre-existing current-projection contract while using the corrected
Qdrant APIs and keeping observation read-only unless ``repair=True``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from .config import StoreConfig
from .postgres import connect
from .qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex

_RETRIEVE_BATCH = 256


def _active_chunk_ids(config: StoreConfig) -> set[str]:
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id
                 FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE d.parser_version=%s AND d.normalization_version=%s
                  AND c.chunker_version=%s""",
            (
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            ),
        )
        return {str(row[0]) for row in cursor.fetchall()}


def _derivation_filter(config: StoreConfig) -> dict[str, Any]:
    return {
        "must": [
            {"key": "parser_version", "match": {"value": config.parser_version}},
            {
                "key": "normalization_version",
                "match": {"value": config.normalization_version},
            },
            {"key": "chunker_version", "match": {"value": config.chunker_version}},
        ]
    }


def _load_postgres_state(config: StoreConfig) -> dict[str, Any]:
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT index_definition_id,index_status,count(*)
                 FROM embedding_manifests
                GROUP BY index_definition_id,index_status"""
        )
        manifests: dict[str, dict[str, int]] = {}
        for definition_id, status, count in cursor.fetchall():
            bucket = manifests.setdefault(str(definition_id), {"total": 0})
            bucket[str(status)] = int(count)
            bucket["total"] += int(count)

        cursor.execute(
            """SELECT index_definition_id,status,count(*)
                 FROM index_jobs
                GROUP BY index_definition_id,status"""
        )
        jobs: dict[str, dict[str, int]] = {}
        for definition_id, status, count in cursor.fetchall():
            bucket = jobs.setdefault(str(definition_id), {"total": 0})
            bucket[str(status)] = int(count)
            bucket["total"] += int(count)

        cursor.execute(
            """SELECT id,fingerprint,physical_collection,lifecycle_status,
                      activated_at,dimension,distance_metric
                 FROM index_definitions ORDER BY created_at"""
        )
        definitions = [
            {
                "id": str(row[0]),
                "fingerprint": row[1],
                "physical_collection": row[2],
                "lifecycle_status": row[3],
                "activated_at": row[4],
                "dimension": row[5],
                "distance_metric": row[6],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """SELECT index_definition_id,point_count,last_verified_at
                 FROM index_point_counts ORDER BY index_definition_id"""
        )
        point_counts = {
            str(row[0]): {"count": int(row[1]), "verified_at": row[2]}
            for row in cursor.fetchall()
        }

    return {
        "manifests": manifests,
        "jobs": jobs,
        "definitions": definitions,
        "point_counts": point_counts,
    }


def _qdrant_for_definition(
    config: StoreConfig, definition: dict[str, Any]
) -> QdrantIndex:
    return QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        definition["physical_collection"],
        int(definition["dimension"]),
        definition["distance_metric"],
    )


def _scroll_ids(index: QdrantIndex, config: StoreConfig) -> set[str]:
    point_ids: set[str] = set()
    offset = None
    while True:
        page = index.point_ids(
            offset,
            limit=_RETRIEVE_BATCH,
            filters=_derivation_filter(config),
        )
        point_ids.update(str(item["id"]) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            return point_ids


def _expected_payloads(config: StoreConfig, point_ids: set[str]) -> dict[str, dict]:
    if not point_ids:
        return {}
    ids = [UUID(value) for value in sorted(point_ids)]
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,d.snapshot_id,d.id AS document_id,s.id AS source_id,
                      s.registered_domain,d.published_at
                 FROM chunks c
                 JOIN documents d ON d.id=c.document_id
                 JOIN asset_snapshots a ON a.id=d.snapshot_id
                 JOIN sources s ON s.id=a.source_id
                WHERE c.id=ANY(%s)""",
            (ids,),
        )
        return {
            str(row[0]): {
                "snapshot_id": str(row[1]) if row[1] is not None else None,
                "document_id": str(row[2]) if row[2] is not None else None,
                "source_id": str(row[3]) if row[3] is not None else None,
                "domain": row[4],
                "published_at": row[5].isoformat() if row[5] is not None else None,
            }
            for row in cursor.fetchall()
        }


def _projection_payload_scan(
    index: QdrantIndex,
    config: StoreConfig,
    point_ids: set[str],
) -> dict[str, Any]:
    """Exhaustively compare identity fields that exist on projection points.

    Historical/manual projection fixtures predate identity payload fields.  In
    projection-only mode an absent field is therefore reported as unavailable,
    not fabricated as a historical mismatch.  If a field is present, drift is
    checked exhaustively.  Run-scoped reconciliation remains strict about both
    missing and mismatched identity fields.
    """
    expected = _expected_payloads(config, point_ids)
    ordered = sorted(point_ids)
    mismatches: list[dict[str, Any]] = []
    unavailable: dict[str, int] = {field: 0 for field in PAYLOAD_INDEX_SCHEMAS}
    retrieved = 0
    batches = 0
    for start in range(0, len(ordered), _RETRIEVE_BATCH):
        batch = ordered[start : start + _RETRIEVE_BATCH]
        batches += 1
        points = index.retrieve(batch, with_payload=True)
        retrieved += len(points)
        for point in points:
            point_id = str(point.get("id"))
            expected_payload = expected.get(point_id)
            if expected_payload is None:
                continue
            payload = point.get("payload") or {}
            for field in PAYLOAD_INDEX_SCHEMAS:
                if field not in payload:
                    unavailable[field] += 1
                    continue
                actual = payload.get(field)
                wanted = expected_payload.get(field)
                actual = str(actual) if actual is not None else None
                wanted = str(wanted) if wanted is not None else None
                if actual != wanted:
                    mismatches.append(
                        {
                            "point_id": point_id,
                            "field": field,
                            "expected": wanted,
                            "actual": actual,
                        }
                    )
    return {
        "complete": retrieved == len(ordered),
        "expected": len(ordered),
        "retrieved": retrieved,
        "batches": batches,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "mismatches_truncated": len(mismatches) > 100,
        "unavailable_fields": unavailable,
    }


def reconcile_projection_compat(
    config: StoreConfig,
    *,
    repair: bool = False,
    index_build: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Reconcile current projection health using the established legacy shape."""
    config.require_database()
    state = _load_postgres_state(config)
    definitions = state["definitions"]
    manifests = state["manifests"]
    jobs = state["jobs"]
    point_counts = state["point_counts"]
    active_ids = _active_chunk_ids(config)

    base_index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.qdrant_alias,
        config.embedding_dimension,
        "Cosine",
    )
    aliases = base_index.list_aliases()
    alias_by_collection = {collection: alias for alias, collection in aliases.items()}

    discrepancies: list[str] = []
    definitions_with_discrepancies: set[str] = set()
    collections: dict[str, dict[str, Any]] = {}

    for definition in definitions:
        definition_id = definition["id"]
        manifest_info = manifests.get(definition_id, {})
        job_info = jobs.get(definition_id, {})
        total_manifests = manifest_info.get("total", 0)
        total_jobs = job_info.get("total", 0)
        complete_manifests = manifest_info.get("complete", 0)
        complete_jobs = job_info.get("complete", 0)

        if total_manifests > 0 and total_jobs == 0:
            discrepancies.append(
                f"definition {definition_id}: {total_manifests} manifests but 0 jobs"
            )
            definitions_with_discrepancies.add(definition_id)
        if complete_manifests != complete_jobs:
            discrepancies.append(
                f"definition {definition_id}: complete manifests "
                f"({complete_manifests}) != complete jobs ({complete_jobs})"
            )
            definitions_with_discrepancies.add(definition_id)

        collection_name = definition["physical_collection"]
        index = _qdrant_for_definition(config, definition)
        try:
            schema = index.inspect_schema()
            if not schema.get("exists") or not schema.get("compatible"):
                discrepancies.append(
                    f"collection {collection_name}: vector schema is missing or incompatible"
                )
                definitions_with_discrepancies.add(definition_id)
                point_ids: set[str] = set()
            else:
                point_ids = _scroll_ids(index, config)

            missing: set[str] = set()
            orphaned: set[str] = set()
            # Preserve the long-standing projection-build contract: an empty
            # not-yet-populated collection is schedulable and does not claim a
            # failed historical run. Partial population is reconciled exactly.
            if point_ids:
                missing = active_ids - point_ids
                orphaned = point_ids - active_ids
                if missing:
                    discrepancies.append(
                        f"collection {collection_name}: {len(missing)} missing points"
                    )
                    definitions_with_discrepancies.add(definition_id)
                if orphaned:
                    discrepancies.append(
                        f"collection {collection_name}: {len(orphaned)} orphaned points"
                    )
                    definitions_with_discrepancies.add(definition_id)

            payload_scan = _projection_payload_scan(index, config, point_ids)
            if not payload_scan["complete"]:
                discrepancies.append(
                    f"collection {collection_name}: payload retrieval incomplete"
                )
                definitions_with_discrepancies.add(definition_id)
            if payload_scan["mismatch_count"]:
                discrepancies.append(
                    f"collection {collection_name}: "
                    f"{payload_scan['mismatch_count']} payload mismatches"
                )
                definitions_with_discrepancies.add(definition_id)

            payload_index_details = index.inspect_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
            payload_indexes = {
                field: detail["compatible"]
                for field, detail in payload_index_details.items()
            }
            if not all(payload_indexes.values()):
                discrepancies.append(
                    f"collection {collection_name}: payload indexes missing or incompatible"
                )
                definitions_with_discrepancies.add(definition_id)

            shard_health = index.inspect_shard_health()
            if not shard_health["healthy"]:
                discrepancies.append(
                    f"collection {collection_name}: shard topology is not fully active"
                )
                definitions_with_discrepancies.add(definition_id)

            alias_name = alias_by_collection.get(collection_name)
            if definition.get("lifecycle_status") == "active" and alias_name is None:
                discrepancies.append(
                    f"collection {collection_name}: active definition is not alias-targeted"
                )
                definitions_with_discrepancies.add(definition_id)

            cached = point_counts.get(definition_id)
            collections[collection_name] = {
                "alias": alias_name,
                "aliases": [alias_name] if alias_name else [],
                "schema": schema,
                "point_count": len(point_ids),
                "cached_point_count": cached["count"] if cached else None,
                "coverage": {
                    "missing": len(missing),
                    "orphaned": len(orphaned),
                },
                "payload_mismatches": payload_scan["mismatches"],
                "payload_scan": payload_scan,
                "payload_indexes": payload_indexes,
                "payload_index_details": payload_index_details,
                "shard_state": shard_health["shards"],
                "shard_health": shard_health,
                "has_discrepancies": definition_id
                in definitions_with_discrepancies,
            }
        except Exception as exc:  # noqa: BLE001
            discrepancies.append(f"collection {collection_name}: {exc}")
            definitions_with_discrepancies.add(definition_id)
            collections[collection_name] = {
                "alias": alias_by_collection.get(collection_name),
                "aliases": [],
                "error": str(exc),
                "has_discrepancies": True,
            }

    repair_actions: list[dict[str, Any]] = []
    repaired: list[str] = []
    repair_errors: list[str] = []
    post_repair = None
    if repair and definitions_with_discrepancies:
        current = next(
            (
                definition
                for definition in definitions
                if definition["fingerprint"] == config.embedding_fingerprint
            ),
            None,
        )
        if current is None:
            repair_errors.append(
                "current embedding fingerprint has no PostgreSQL index definition"
            )
        elif index_build is None:
            repair_errors.append("projection repair requires the index-build callback")
        else:
            try:
                action = index_build(config, document_id=None, repair_orphans=True)
                repair_actions.append(action)
                repaired.append(current["id"])
            except Exception as exc:  # noqa: BLE001
                repair_errors.append(f"repair failed for {current['id']}: {exc}")
        post_repair = reconcile_projection_compat(
            config,
            repair=False,
            index_build=index_build,
        )

    result = {
        "schema_version": "qdrant-projection-reconciliation-v2",
        "scope": "projection",
        "authoritative_membership": False,
        "read_only": not repair,
        "definitions": definitions,
        "manifests": manifests,
        "jobs": jobs,
        "total_active_chunks": len(active_ids),
        "qdrant": {
            "ok": not discrepancies,
            "aliases": aliases,
            "collections": collections,
        },
        "discrepancies": discrepancies,
        "ok": not discrepancies,
        "repaired": repaired,
        "repair_actions": repair_actions,
        "repair_errors": repair_errors,
        "post_repair": post_repair,
    }
    if repair and post_repair is not None:
        # Legacy callers expect the top-level observation to represent the
        # state *after* explicit repair. Preserve repair evidence while
        # promoting the fresh read-only observation to the public result.
        result.update(post_repair)
        result["read_only"] = False
        result["repaired"] = repaired
        result["repair_actions"] = repair_actions
        result["repair_errors"] = repair_errors
        result["post_repair"] = post_repair
        if repair_errors:
            result["ok"] = False
            result["discrepancies"] = [
                *result.get("discrepancies", []),
                *repair_errors,
            ]
    return result
