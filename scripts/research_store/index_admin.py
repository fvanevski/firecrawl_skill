from __future__ import annotations

from functools import partial
from typing import Any
from uuid import UUID

from .indexing import IndexWorker, OpenAICompatibleEmbedder
from .postgres import PostgresUnitOfWork
from .qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex
from .qdrant_authority import read_required_alias_state
from .valkey_queue import ValkeyQueue
from .store_runtime import database


def uow_factory(config):
    return partial(
        PostgresUnitOfWork,
        config.database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
    )


def qdrant(config, collection=None, dimension=None, distance="Cosine"):
    return QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        collection or config.qdrant_alias,
        dimension or config.embedding_dimension,
        distance,
    )


def worker(config):
    if not config.embedding_url:
        raise RuntimeError("EMBEDDING_URL is required to process index jobs")
    return IndexWorker(
        uow_factory(config),
        qdrant(config),
        OpenAICompatibleEmbedder(
            config.embedding_url,
            config.embedding_model,
            config.embedding_api_key,
            config.embedding_dimension,
            config.embedding_fingerprint,
        ),
        queue=ValkeyQueue(config.valkey_url),
        lease_seconds=config.job_lease_seconds,
        max_attempts=config.max_index_attempts,
    )


def index_rows(config) -> list[dict[str, Any]]:
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT d.id,d.fingerprint,d.physical_collection,d.model_name,
            d.model_revision,d.dimension,d.distance_metric,d.normalization,
            d.instruction_template_hash,d.lifecycle_status,d.created_at,d.activated_at,
            count(m.id),count(m.id) FILTER(WHERE m.index_status='complete')
            FROM index_definitions d
            LEFT JOIN embedding_manifests m ON m.index_definition_id=d.id
            GROUP BY d.id ORDER BY d.created_at DESC"""
        )
        keys = (
            "id", "fingerprint", "physical_collection", "model_name",
            "model_revision", "dimension", "distance_metric", "normalization",
            "instruction_template_hash", "lifecycle_status", "created_at",
            "activated_at", "manifest_count", "complete_count",
        )
        return [dict(zip(keys, row)) for row in cur.fetchall()]


def active_chunk_ids(config, document_id=None) -> set[UUID]:
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE d.parser_version=%s AND d.normalization_version=%s
              AND c.chunker_version=%s
              AND (%s::uuid IS NULL OR c.document_id=%s::uuid)
            ORDER BY c.id""",
            (
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
                document_id,
                document_id,
            ),
        )
        return {row[0] for row in cur.fetchall()}


def derivation_filter(config) -> dict[str, list[dict[str, Any]]]:
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


def index_build(config, document_id=None, *, repair_orphans=False):
    with uow_factory(config)() as uow:
        definition = uow.ensure_index_definition()
    index = qdrant(
        config,
        definition["physical_collection"],
        definition["dimension"],
        definition["distance_metric"],
    )
    schema = index.ensure_schema()
    selected_chunk_ids = active_chunk_ids(config, document_id)
    indexed_ids: set[UUID] = set()
    offset = None
    while True:
        page = index.point_ids(offset, filters=derivation_filter(config))
        indexed_ids.update(UUID(str(item["id"])) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    missing_chunk_ids = selected_chunk_ids - indexed_ids
    orphaned_chunk_ids = indexed_ids - selected_chunk_ids if document_id is None else set()
    deleted_orphaned = 0
    if repair_orphans and orphaned_chunk_ids:
        index.delete(sorted(orphaned_chunk_ids, key=str))
        deleted_orphaned = len(orphaned_chunk_ids)

    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO embedding_manifests(
            chunk_id,model_name,model_revision,dimension,distance_metric,
            normalization,instruction_template_hash,qdrant_collection,
            qdrant_point_id,index_status,index_definition_id)
            SELECT c.id,%s,%s,%s,%s,%s,%s,%s,c.id,'pending',%s
            FROM chunks c WHERE c.id=ANY(%s)
            ON CONFLICT(chunk_id,index_definition_id) DO UPDATE
            SET qdrant_collection=excluded.qdrant_collection
            RETURNING id,chunk_id,index_status""",
            (
                definition["model_name"], definition["model_revision"],
                definition["dimension"], definition["distance_metric"],
                definition["normalization"], definition["instruction_template_hash"],
                definition["physical_collection"], definition["id"],
                list(selected_chunk_ids),
            ),
        )
        manifests = cur.fetchall()
        manifest_ids = [row[0] for row in manifests]
        if manifest_ids:
            cur.execute(
                """SELECT manifest_id FROM index_jobs
                WHERE manifest_id=ANY(%s) AND operation='upsert'""",
                (manifest_ids,),
            )
            job_manifest_ids = {row[0] for row in cur.fetchall()}
        else:
            job_manifest_ids = set()
        missing_job_manifest_ids = set(manifest_ids) - job_manifest_ids
        requeue_ids = [
            row[0]
            for row in manifests
            if row[2] != "complete"
            or row[1] in missing_chunk_ids
            or row[0] in missing_job_manifest_ids
        ]
        cur.execute(
            """INSERT INTO index_jobs(
            entity_type,entity_id,index_name,operation,status,manifest_id,index_definition_id)
            SELECT 'chunk',m.chunk_id,%s,'upsert','pending',m.id,%s
            FROM embedding_manifests m WHERE m.id=ANY(%s)
            ON CONFLICT(manifest_id,operation) DO NOTHING""",
            (definition["physical_collection"], definition["id"], manifest_ids),
        )
        if requeue_ids:
            cur.execute(
                """UPDATE index_jobs SET status='pending',available_at=now(),
                attempt_count=0,started_at=NULL,completed_at=NULL,error=NULL,
                lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE manifest_id=ANY(%s) AND operation='upsert'""",
                (requeue_ids,),
            )
            cur.execute(
                """UPDATE embedding_manifests SET index_status='pending',
                indexed_at=NULL,error=NULL WHERE id=ANY(%s)""",
                (requeue_ids,),
            )
        cur.execute(
            """SELECT count(*) FROM index_jobs
            WHERE manifest_id=ANY(%s) AND operation='upsert' AND status='pending'""",
            (requeue_ids,),
        )
        pending_verified = cur.fetchone()[0]
        if pending_verified != len(requeue_ids):
            raise RuntimeError(
                f"index-build reconciliation failed: expected {len(requeue_ids)} "
                f"pending jobs for requeued manifests, but only {pending_verified} "
                "are pending.  Manifests may be orphaned or have mismatched "
                "index_definition_id."
            )
        cur.execute(
            """INSERT INTO index_point_counts(index_definition_id,point_count,last_verified_at)
            VALUES(%s,%s,now()) ON CONFLICT(index_definition_id) DO UPDATE SET
            point_count=excluded.point_count,last_verified_at=excluded.last_verified_at""",
            (definition["id"], len(selected_chunk_ids)),
        )
    queue = ValkeyQueue(config.valkey_url)
    if requeue_ids:
        queue.notify(requeue_ids[0])
    result = {
        "index_definition": definition,
        "selected_chunks": len(manifest_ids),
        "scheduled": len(requeue_ids),
        "missing_points": len(missing_chunk_ids),
        "orphaned_points": len(orphaned_chunk_ids),
        "deleted_orphaned": deleted_orphaned,
        "missing_jobs": len(missing_job_manifest_ids),
        "qdrant_schema": schema,
    }
    result["payload_indexes"] = index.ensure_payload_indexes(
        PAYLOAD_INDEX_SCHEMAS, create_missing=True
    )
    return result


def recover_activation(config) -> list[str]:
    aliases = qdrant(config).list_aliases()
    active_collection = aliases.get(config.qdrant_alias)
    recovered: list[str] = []
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT j.id,j.target_definition_id,d.physical_collection
            FROM index_activation_journal j
            JOIN index_definitions d ON d.id=j.target_definition_id
            WHERE j.status IN ('prepared','switched') ORDER BY j.created_at"""
        )
        for journal_id, definition_id, collection in cur.fetchall():
            if active_collection == collection:
                cur.execute(
                    "UPDATE index_definitions SET lifecycle_status='inactive' WHERE lifecycle_status='active' AND id<>%s",
                    (definition_id,),
                )
                cur.execute(
                    "UPDATE index_definitions SET lifecycle_status='active',activated_at=now() WHERE id=%s",
                    (definition_id,),
                )
                cur.execute(
                    "UPDATE index_activation_journal SET status='complete',updated_at=now() WHERE id=%s",
                    (journal_id,),
                )
                recovered.append(str(journal_id))
            else:
                cur.execute(
                    """UPDATE index_activation_journal SET status='failed',updated_at=now(),
                    error='alias did not switch to prepared target' WHERE id=%s""",
                    (journal_id,),
                )
    return recovered


def activate_index(config, identifier, action):
    recovered = recover_activation(config)
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id,physical_collection,dimension,distance_metric
            FROM index_definitions WHERE id=%s""",
            (UUID(identifier),),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit("index definition not found")
        definition_id, collection, dimension, distance = row
        chunks = active_chunk_ids(config)
        total_chunks = len(chunks)
        cur.execute(
            """SELECT count(*) FROM embedding_manifests m
            JOIN chunks c ON c.id=m.chunk_id JOIN documents d ON d.id=c.document_id
            WHERE m.index_definition_id=%s AND m.index_status='complete'
              AND d.parser_version=%s AND d.normalization_version=%s
              AND c.chunker_version=%s""",
            (
                definition_id, config.parser_version,
                config.normalization_version, config.chunker_version,
            ),
        )
        complete = cur.fetchone()[0]
        if complete != total_chunks:
            raise SystemExit(
                f"index coverage incomplete: {complete} complete manifests for {total_chunks} chunks"
            )
        cur.execute(
            "SELECT id FROM index_definitions WHERE lifecycle_status='active' LIMIT 1"
        )
        previous = cur.fetchone()
    index = qdrant(config, collection, dimension, distance)
    schema = index.inspect_schema()
    if not schema["exists"] or not schema["compatible"]:
        raise SystemExit(f"target collection schema is not compatible: {schema}")
    point_ids: set[str] = set()
    offset = None
    while True:
        page = index.point_ids(offset, filters=derivation_filter(config))
        point_ids.update(str(item["id"]) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    chunk_ids = {str(value) for value in chunks}
    if point_ids != chunk_ids:
        raise SystemExit(
            f"Qdrant coverage mismatch: missing={len(chunk_ids - point_ids)} "
            f"orphaned={len(point_ids - chunk_ids)}"
        )
    if total_chunks:
        index.search([1.0] + [0.0] * (dimension - 1), {}, 1)
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO index_activation_journal(
            target_definition_id,previous_definition_id,action)
            VALUES(%s,%s,%s) RETURNING id""",
            (definition_id, previous[0] if previous else None, action),
        )
        journal_id = cur.fetchone()[0]
    switched = index.switch_alias(config.qdrant_alias, collection)
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE index_activation_journal SET status='switched',updated_at=now() WHERE id=%s",
            (journal_id,),
        )
        cur.execute(
            "UPDATE index_definitions SET lifecycle_status='inactive' WHERE lifecycle_status='active' AND id<>%s",
            (definition_id,),
        )
        cur.execute(
            "UPDATE index_definitions SET lifecycle_status='active',activated_at=now() WHERE id=%s",
            (definition_id,),
        )
        cur.execute(
            "UPDATE index_activation_journal SET status='complete',updated_at=now() WHERE id=%s",
            (journal_id,),
        )
    return {
        "action": action,
        "index_definition_id": definition_id,
        "collection": collection,
        "alias": config.qdrant_alias,
        "switched": switched,
        "recovered_journals": recovered,
        "coverage": total_chunks,
    }


def qdrant_alias_state(config):
    return read_required_alias_state(config)


def list_index_state(config) -> dict[str, Any]:
    return {
        "alias": config.qdrant_alias,
        "aliases": qdrant(config).list_aliases(),
        "definitions": index_rows(config),
    }


def prune_indexes(config, *, dry_run: bool, force: bool, keep_last: int, index_id: str | None) -> dict[str, Any]:
    if dry_run and force:
        raise SystemExit("--dry-run and --force are mutually exclusive")
    if force and not index_id:
        raise SystemExit("--force requires --index-id for an exact prune target")
    if keep_last < 0:
        raise SystemExit("--keep-last must be non-negative")
    aliases = qdrant(config).list_aliases()
    active = aliases.get(config.qdrant_alias)
    rows = index_rows(config)
    if index_id:
        rows = [row for row in rows if str(row["id"]) == index_id]
        if not rows:
            raise SystemExit("index definition not found")
    else:
        rows = rows[keep_last:]
    candidates = [row for row in rows if row["physical_collection"] != active]
    result = {
        "action": "deleted" if force else "dry_run",
        "indexes": [
            {"id": row["id"], "collection": row["physical_collection"]}
            for row in candidates
        ],
    }
    if force:
        for row in candidates:
            qdrant(
                config,
                row["physical_collection"],
                row["dimension"],
                row["distance_metric"],
            ).delete_collection()
    return result


def prune_cache(config) -> int:
    return ValkeyQueue(config.valkey_url).prune_cache()
