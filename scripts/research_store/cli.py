from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from .blob import ContentAddressedBlobStore
from .compat import export_json, import_scratch
from .config import StoreConfig
from .container import build_run_service, build_service
from .domain import IngestRequest
from .indexing import IndexWorker, OpenAICompatibleEmbedder
from .postgres import PostgresUnitOfWork, connect
from .qdrant import QdrantIndex
from .queue import ValkeyQueue
from .retrieval import CohereCompatibleReranker
from .service import dumps


def parser():
    root = argparse.ArgumentParser(
        prog="research-db", description="Authoritative research asset store"
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("ingest-ready")

    imp = sub.add_parser("import-scratch")
    imp.add_argument("path", nargs="?")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--report")
    ingest = sub.add_parser("ingest-result")
    ingest.add_argument("--url", required=True)
    ingest.add_argument("--file", required=True)
    ingest.add_argument("--title")
    ingest.add_argument("--metadata-json", default="{}")
    sub.add_parser("verify-blobs")

    worker = sub.add_parser("worker")
    worker.add_argument("--batch-size", type=int, default=32)
    worker.add_argument("--poll-seconds", type=float)
    worker.add_argument("--lease-seconds", type=int)
    worker.add_argument("--max-attempts", type=int)
    worker.add_argument("--once", action="store_true")
    once = sub.add_parser("index-once")
    once.add_argument("--limit", type=int, default=64)

    sub.add_parser("index-list")
    build = sub.add_parser("index-build")
    build.add_argument("--current-config", action="store_true", required=True)
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document")
    reindex = sub.add_parser("reindex")
    legacy_selection = reindex.add_mutually_exclusive_group(required=True)
    legacy_selection.add_argument("--all", action="store_true")
    legacy_selection.add_argument("--document")
    activate = sub.add_parser("index-activate")
    activate.add_argument("id")
    rollback = sub.add_parser("index-rollback")
    rollback.add_argument("id")
    prune = sub.add_parser("index-prune")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--force", action="store_true")
    prune.add_argument("--keep-last", type=int, default=2)
    prune.add_argument("--index-id")
    sub.add_parser("reconcile-qdrant")
    sub.add_parser("prune-cache")

    rederive = sub.add_parser("rederive")
    target = rederive.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true")
    target.add_argument("--snapshot")
    export = sub.add_parser("export-invocation")
    export.add_argument("invocation_id")
    export.add_argument("--output", required=True)
    export_run = sub.add_parser("export-run")
    export_run.add_argument("id")
    export_run.add_argument("--output", required=True)

    run_start = sub.add_parser("run-start")
    run_start.add_argument("external_id")
    run_start.add_argument("objective")
    run_start.add_argument("--catalog-pointer")
    run_start.add_argument(
        "--mode",
        choices=("agent_led", "autonomous_local", "deterministic_debug"),
        default="autonomous_local",
    )
    run_start.add_argument("--idempotency-key")
    run_start.add_argument("--actor", default="cli")
    run_status = sub.add_parser("run-status")
    run_status.add_argument("external_id")
    run_mode = sub.add_parser("run-mode-change")
    run_mode.add_argument("external_id")
    run_mode.add_argument(
        "mode", choices=("agent_led", "autonomous_local", "deterministic_debug")
    )
    run_mode.add_argument("--expected-revision", type=int, required=True)
    run_mode.add_argument("--idempotency-key", required=True)
    run_mode.add_argument("--requested-by", required=True)
    run_mode.add_argument("--approved-by", required=True)
    run_mode.add_argument("--reason", required=True)
    run_mode.add_argument("--actor", default="operator")
    run_mode.add_argument("--actor-identifier")
    run_transition = sub.add_parser("run-transition")
    run_transition.add_argument("external_id")
    run_transition.add_argument("next_state")
    run_transition.add_argument("--expected-revision", type=int, required=True)
    run_transition.add_argument("--idempotency-key", required=True)
    run_transition.add_argument("--actor", default="cli")
    run_transition.add_argument("--actor-identifier")
    run_transition.add_argument("--semantic-proposal-id")
    run_transition.add_argument("--reason")
    run_finish = sub.add_parser("run-finish")
    run_finish.add_argument("external_id")
    run_finish.add_argument("--outcome", required=True)
    run_finish.add_argument(
        "--status", choices=("complete", "failed"), default="complete"
    )
    run_finish.add_argument("--catalog-pointer")
    run_finish.add_argument("--source-manifest-sha256")
    run_finish.add_argument("--answer-sha256")
    run_finish.add_argument("--expected-revision", type=int)
    run_finish.add_argument("--idempotency-key")
    run_finish.add_argument("--actor", default="cli")
    run_reopen = sub.add_parser("run-reopen")
    run_reopen.add_argument("external_id")
    run_reopen.add_argument("--reason", default="legacy compatibility reopen")
    run_reopen.add_argument("--expected-revision", type=int)
    run_reopen.add_argument("--idempotency-key")
    run_reopen.add_argument("--actor", default="cli")
    run_cancel = sub.add_parser("run-cancel")
    run_cancel.add_argument("external_id")
    run_cancel.add_argument("--reason", default="cancelled by operator")
    run_cancel.add_argument("--expected-revision", type=int)
    run_cancel.add_argument("--idempotency-key")
    run_cancel.add_argument("--actor", default="cli")
    budget_record = sub.add_parser("budget-record")
    budget_record.add_argument("external_id")
    budget_record.add_argument("--research-spec", required=True)
    budget_record.add_argument("--budget-snapshot", required=True)
    comparisons = sub.add_parser("legacy-comparisons")
    comparisons.add_argument("--research-run-id")
    comparisons.add_argument("--invocation-id")
    comparisons.add_argument(
        "--entry-point", choices=("frun", "fsearch_smart", "fsearch", "fscrape")
    )
    comparisons.add_argument("--divergent-only", action="store_true")
    comparisons.add_argument("--limit", type=int, default=100)

    search_plan_rec = sub.add_parser("search-plan-record")
    search_plan_rec.add_argument("external_id")
    search_plan_rec.add_argument("--research-spec-id", required=True)
    search_plan_rec.add_argument("--revision", type=int, required=True)
    search_plan_rec.add_argument("--search-plan", required=True)
    search_plan_rec.add_argument("--idempotency-key", required=True)

    search_plan_get = sub.add_parser("search-plan-get")
    search_plan_get.add_argument("external_id")
    search_plan_get.add_argument("--plan-id")
    search_plan_get.add_argument("--revision", type=int)

    plan_query_get = sub.add_parser("search-plan-query-get")
    plan_query_get.add_argument("query_id")

    search_resp_rec = sub.add_parser("search-response-record")
    search_resp_rec.add_argument("external_id")
    search_resp_rec.add_argument("--query-text", required=True)
    search_resp_rec.add_argument("--backend", default="firecrawl")
    search_resp_rec.add_argument("--payload-file", help="Path to raw payload file (reads stdin if omitted)")
    search_resp_rec.add_argument("--idempotency-key", required=True)
    search_resp_rec.add_argument("--plan-id")
    search_resp_rec.add_argument("--plan-query-id")
    search_resp_rec.add_argument("--provider-request-id")
    search_resp_rec.add_argument("--parser-version", default="firecrawl-search-v1")
    search_resp_rec.add_argument("--http-status", type=int)

    search_resp_get = sub.add_parser("search-response-get")
    search_resp_get.add_argument("response_id")

    search_resp_replay = sub.add_parser("search-response-replay")
    search_resp_replay.add_argument("response_id")

    cand_rec_resp = sub.add_parser("candidate-record-response")
    cand_rec_resp.add_argument("external_id")
    cand_rec_resp.add_argument("--search-response-id", required=True)

    cand_get = sub.add_parser("candidate-get")
    cand_get.add_argument("candidate_id")

    cand_list = sub.add_parser("candidate-list")
    cand_list.add_argument("external_id")
    cand_list.add_argument("--domain")
    cand_list.add_argument("--min-recurrence", type=int)
    cand_list.add_argument("--duplicate-group-id")

    cand_occ_list = sub.add_parser("candidate-occurrences-list")
    cand_occ_list.add_argument("candidate_id")

    cand_grp = sub.add_parser("candidate-assign-group")
    cand_grp.add_argument("candidate_ids", nargs="+")
    cand_grp.add_argument("--group-id")




    sub.add_parser("corpus-overview")
    search = sub.add_parser("search-assets")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--domain")
    search.add_argument("--source-type")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    search.add_argument("--research-run-id")
    inspect = sub.add_parser("inspect-asset")
    inspect.add_argument("id")
    fetch = sub.add_parser("fetch-passages")
    fetch.add_argument("ids", nargs="+")
    fetch.add_argument("--max-tokens", type=int, default=2000)
    fetch.add_argument("--max-passages", type=int, default=8)
    fetch.add_argument("--research-run-id")
    expand = sub.add_parser("expand-relationships")
    expand.add_argument("ids", nargs="+")
    expand.add_argument("--max-hops", type=int, default=1)
    expand.add_argument("--max-results", type=int, default=50)
    expand.add_argument("--max-tokens", type=int, default=2000)
    packet = sub.add_parser("build-evidence-packet")
    packet.add_argument("ids", nargs="+")
    packet.add_argument("--max-tokens", type=int, default=3000)
    return root


def _db(config):
    config.require_database()
    return connect(config.database_url)


def _uow_factory(config):
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


def _qdrant(config, collection=None, dimension=None, distance="Cosine"):
    return QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        collection or config.qdrant_alias,
        dimension or config.embedding_dimension,
        distance,
    )


def _worker(config):
    if not config.embedding_url:
        raise RuntimeError("EMBEDDING_URL is required to process index jobs")
    return IndexWorker(
        _uow_factory(config),
        _qdrant(config),
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


def _schema_state(config):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).parents[2] / "alembic.ini"
    head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        current = row[0] if row else None
    return {"current": current, "head": head, "at_head": current == head}


def _resolve_run_id(config, external_id):
    if not external_id:
        return None
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,status FROM research_runs WHERE external_run_id=%s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"research run not found: {external_id}")
    if row[1] != "running":
        raise SystemExit(f"research run is finished; reopen it first: {external_id}")
    return row[0]


def _index_rows(config):
    with _db(config) as conn, conn.cursor() as cur:
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
            "id",
            "fingerprint",
            "physical_collection",
            "model_name",
            "model_revision",
            "dimension",
            "distance_metric",
            "normalization",
            "instruction_template_hash",
            "lifecycle_status",
            "created_at",
            "activated_at",
            "manifest_count",
            "complete_count",
        )
        return [dict(zip(keys, row)) for row in cur.fetchall()]


def _active_chunk_ids(config, document_id=None):
    with _db(config) as conn, conn.cursor() as cur:
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


def _derivation_filter(config):
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


def _index_build(config, document_id=None):
    with _uow_factory(config)() as uow:
        definition = uow.ensure_index_definition()
    index = _qdrant(
        config,
        definition["physical_collection"],
        definition["dimension"],
        definition["distance_metric"],
    )
    schema = index.ensure_schema()
    selected_chunk_ids = _active_chunk_ids(config, document_id)
    indexed_ids, offset = set(), None
    while True:
        page = index.point_ids(offset, filters=_derivation_filter(config))
        indexed_ids.update(UUID(str(item["id"])) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    with _db(config) as conn, conn.cursor() as cur:
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
                definition["model_name"],
                definition["model_revision"],
                definition["dimension"],
                definition["distance_metric"],
                definition["normalization"],
                definition["instruction_template_hash"],
                definition["physical_collection"],
                definition["id"],
                list(selected_chunk_ids),
            ),
        )
        manifests = cur.fetchall()
        manifest_ids = [row[0] for row in manifests]
        requeue_ids = [
            row[0]
            for row in manifests
            if row[1] not in indexed_ids or row[2] != "complete"
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
                started_at=NULL,completed_at=NULL,error=NULL,lease_token=NULL,
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE manifest_id=ANY(%s) AND operation='upsert'""",
                (requeue_ids,),
            )
            cur.execute(
                """UPDATE embedding_manifests SET index_status='pending',
                indexed_at=NULL,error=NULL WHERE id=ANY(%s)""",
                (requeue_ids,),
            )
    queue = ValkeyQueue(config.valkey_url)
    if manifest_ids:
        queue.notify(manifest_ids[0])
    return {
        "index_definition": definition,
        "selected_chunks": len(manifest_ids),
        "scheduled": len(requeue_ids),
        "qdrant_schema": schema,
    }


def _recover_activation(config):
    aliases = _qdrant(config).list_aliases()
    active_collection = aliases.get(config.qdrant_alias)
    recovered = []
    with _db(config) as conn, conn.cursor() as cur:
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


def _activate_index(config, identifier, action):
    recovered = _recover_activation(config)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id,physical_collection,dimension,distance_metric
            FROM index_definitions WHERE id=%s""",
            (UUID(identifier),),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit("index definition not found")
        definition_id, collection, dimension, distance = row
        active_chunk_ids = _active_chunk_ids(config)
        total_chunks = len(active_chunk_ids)
        cur.execute(
            """SELECT count(*) FROM embedding_manifests m
            JOIN chunks c ON c.id=m.chunk_id JOIN documents d ON d.id=c.document_id
            WHERE m.index_definition_id=%s AND m.index_status='complete'
              AND d.parser_version=%s AND d.normalization_version=%s
              AND c.chunker_version=%s""",
            (
                definition_id,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
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
    index = _qdrant(config, collection, dimension, distance)
    schema = index.inspect_schema()
    if not schema["exists"] or not schema["compatible"]:
        raise SystemExit(f"target collection schema is not compatible: {schema}")
    point_ids, offset = set(), None
    while True:
        page = index.point_ids(offset, filters=_derivation_filter(config))
        point_ids.update(str(item["id"]) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    chunk_ids = {str(value) for value in active_chunk_ids}
    if point_ids != chunk_ids:
        raise SystemExit(
            f"Qdrant coverage mismatch: missing={len(chunk_ids - point_ids)} orphaned={len(point_ids - chunk_ids)}"
        )
    if total_chunks:
        index.search([1.0] + [0.0] * (dimension - 1), {}, 1)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO index_activation_journal(
            target_definition_id,previous_definition_id,action)
            VALUES(%s,%s,%s) RETURNING id""",
            (definition_id, previous[0] if previous else None, action),
        )
        journal_id = cur.fetchone()[0]
    switched = index.switch_alias(config.qdrant_alias, collection)
    with _db(config) as conn, conn.cursor() as cur:
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


def _blob_health(config):
    store = ContentAddressedBlobStore(config.blob_root)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,content_sha256 FROM asset_snapshots")
        references = {digest: snapshot_id for snapshot_id, digest in cur.fetchall()}
    missing = [
        {"snapshot_id": references[digest], "sha256": digest}
        for digest in references
        if not store.verify(digest)
    ]
    disk_hashes = {
        path.name
        for path in config.blob_root.rglob("*")
        if path.is_file()
        and len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    }
    return {
        "ok": not missing and not (disk_hashes - references.keys()),
        "referenced": len(references),
        "missing_or_corrupt": missing,
        "unreferenced": sorted(disk_hashes - references.keys()),
    }


def _doctor(config):
    checks, failed = {}, False
    try:
        checks["schema"] = _schema_state(config)
        if not checks["schema"]["at_head"]:
            failed = True
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status,count(*) FROM index_jobs GROUP BY status ORDER BY status"
            )
            checks["index_jobs"] = dict(cur.fetchall())
            if checks["schema"]["at_head"]:
                cur.execute(
                    """SELECT count(*) FILTER(WHERE status IN ('partial','failed')),
                    min(started_at) FILTER(WHERE status='running') FROM ingestion_batches"""
                )
                bad, oldest_running = cur.fetchone()
                checks["ingestion_batches"] = {
                    "partial_or_failed": bad,
                    "oldest_running": oldest_running,
                }
        if checks["schema"]["at_head"]:
            with _uow_factory(config)() as uow:
                checks["worker"] = uow.worker_status()
            workers = checks["worker"]["workers"]
            threshold = max(90, config.worker_poll_seconds * 4)
            age = (
                (
                    datetime.now(timezone.utc) - workers[0]["heartbeat_at"]
                ).total_seconds()
                if workers
                else None
            )
            checks["worker"]["latest_heartbeat_age_seconds"] = (
                round(age, 3) if age is not None else None
            )
            checks["worker"]["heartbeat_freshness_threshold_seconds"] = threshold
            checks["worker"]["current_worker_available"] = (
                age is not None and age <= threshold
            ) or checks["worker"]["active_leases"] > 0
            if checks["worker"]["dead_jobs"] or checks["worker"]["stale_leases"]:
                failed = True
            if not checks["worker"]["current_worker_available"]:
                failed = True
        else:
            checks["worker"] = {"available": False, "reason": "migration required"}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        if not config.blob_root.is_dir():
            raise RuntimeError(f"blob root is not a directory: {config.blob_root}")
        if not os.access(config.blob_root, os.R_OK | os.X_OK):
            raise RuntimeError("blob root is not readable")
        checks["blobs"] = _blob_health(config)
        failed |= not checks["blobs"]["ok"]
    except Exception as exc:
        checks["blobs"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        aliases = _qdrant(config).list_aliases()
        active = aliases.get(config.qdrant_alias)
        qdrant = {"ok": True, "alias": config.qdrant_alias, "collection": active}
        if active:
            if not checks.get("schema", {}).get("at_head"):
                qdrant["schema"] = _qdrant(config, active).inspect_schema()
                checks["qdrant"] = qdrant
                active = None
        if active:
            rows = [
                row
                for row in _index_rows(config)
                if row["physical_collection"] == active
            ]
            if not rows:
                raise RuntimeError("active alias is not backed by an index definition")
            row = rows[0]
            qdrant["query_embedding_compatible"] = (
                row["fingerprint"] == config.embedding_fingerprint
            )
            if not qdrant["query_embedding_compatible"]:
                failed = True
            qdrant["schema"] = _qdrant(
                config, active, row["dimension"], row["distance_metric"]
            ).inspect_schema()
            if not qdrant["schema"]["compatible"]:
                failed = True
            if checks.get("schema", {}).get("at_head"):
                point_ids, offset = set(), None
                active_index = _qdrant(
                    config, active, row["dimension"], row["distance_metric"]
                )
                while True:
                    page = active_index.point_ids(
                        offset, filters=_derivation_filter(config)
                    )
                    point_ids.update(str(item["id"]) for item in page.get("points", []))
                    offset = page.get("next_page_offset")
                    if not offset:
                        break
                chunk_ids = {str(value) for value in _active_chunk_ids(config)}
                qdrant["coverage"] = {
                    "missing": len(chunk_ids - point_ids),
                    "orphaned": len(point_ids - chunk_ids),
                }
                failed |= point_ids != chunk_ids
        checks["qdrant"] = qdrant
    except Exception as exc:
        checks["qdrant"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        import redis

        checks["valkey"] = {"ok": bool(redis.Redis.from_url(config.valkey_url).ping())}
    except Exception as exc:
        checks["valkey"] = {"ok": False, "error": str(exc)}
        failed = True

    for name, endpoint in (
        ("embedding", config.embedding_url),
        ("reranker", config.reranker_url),
    ):
        try:
            if not endpoint:
                raise RuntimeError(f"{name.upper()}_URL is not configured")
            if name == "embedding":
                vector = OpenAICompatibleEmbedder(
                    endpoint,
                    config.embedding_model,
                    config.embedding_api_key,
                    config.embedding_dimension,
                )("research-store-doctor")
                checks[name] = {"ok": True, "dimension": len(vector)}
            else:
                ranked = CohereCompatibleReranker(
                    endpoint, config.reranker_model, config.reranker_api_key
                )(
                    "research database",
                    [
                        {"candidate_id": "relevant", "excerpt": "research database"},
                        {"candidate_id": "other", "excerpt": "yellow bananas"},
                    ],
                )
                if not ranked or ranked[0]["candidate_id"] != "relevant":
                    raise RuntimeError("unexpected reranker ordering")
                checks[name] = {"ok": True}
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)}
            failed = True
    checks["configuration"] = {
        "embedding_fingerprint": config.embedding_fingerprint,
        "physical_collection": config.physical_collection,
        "normalization_version": config.normalization_version,
        "parser_version": config.parser_version,
        "chunker_version": config.chunker_version,
    }
    return checks, failed


def main(argv=None):
    args = parser().parse_args(argv)
    config = StoreConfig.from_env()

    if args.command == "migrate":
        config.require_database()
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as exc:
            raise RuntimeError(
                "migrations require dependencies from requirements-research-store.txt"
            ) from exc
        ini = Path(__file__).parents[2] / "alembic.ini"
        command.upgrade(Config(str(ini)), "head")
        print(dumps(_schema_state(config)))
        return 0
    if args.command == "status":
        schema = _schema_state(config)
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT status,count(*) FROM index_jobs GROUP BY status")
            jobs = dict(cur.fetchall())
            if schema["at_head"]:
                cur.execute(
                    "SELECT status,count(*) FROM ingestion_batches GROUP BY status"
                )
                batches = dict(cur.fetchall())
            else:
                batches = {"available": False, "reason": "migration required"}
        print(dumps({"schema": schema, "index_jobs": jobs, "batches": batches}))
        return 0 if schema["at_head"] else 1
    if args.command == "doctor":
        checks, failed = _doctor(config)
        print(dumps(checks))
        return 1 if failed else 0
    if args.command == "ingest-ready":
        schema = _schema_state(config)
        if not schema["at_head"]:
            raise SystemExit(
                f"research store migration required: {schema['current']} != {schema['head']}"
            )
        if not config.blob_root.is_dir():
            raise SystemExit(f"blob root is not writable: {config.blob_root}")
        with _db(config) as conn, conn.cursor() as cur:
            required_privileges = {
                "sources": ("SELECT", "INSERT", "UPDATE"),
                "asset_snapshots": ("SELECT", "INSERT"),
                "documents": ("SELECT", "INSERT"),
                "document_blocks": ("SELECT", "INSERT"),
                "chunks": ("SELECT", "INSERT"),
                "embedding_manifests": ("SELECT", "INSERT", "UPDATE"),
                "index_definitions": ("SELECT", "INSERT", "UPDATE"),
                "index_jobs": ("SELECT", "INSERT", "UPDATE"),
                "ingestion_batches": ("SELECT", "INSERT", "UPDATE"),
                "ingestion_batch_assets": (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                ),
                "research_runs": ("SELECT", "INSERT", "UPDATE"),
                "research_run_assets": ("SELECT", "INSERT", "UPDATE"),
                "retrieval_events": ("SELECT", "INSERT"),
            }
            missing = []
            for table, privileges in required_privileges.items():
                for privilege in privileges:
                    cur.execute(
                        "SELECT has_table_privilege(current_user,%s,%s)",
                        (f"public.{table}", privilege),
                    )
                    if not cur.fetchone()[0]:
                        missing.append(f"{table}:{privilege}")
            if missing:
                raise SystemExit(
                    "database role lacks corpus privileges: " + ", ".join(missing)
                )
            probe_url = f"https://ingest-ready.invalid/{uuid4()}"
            cur.execute(
                """INSERT INTO sources(canonical_url,metadata)
                VALUES(%s,%s) ON CONFLICT(canonical_url) DO UPDATE
                SET metadata=sources.metadata || excluded.metadata""",
                (probe_url, json.dumps({"ingest_ready_probe": True})),
            )
            conn.rollback()
        probe_path = renamed_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=config.blob_root,
                prefix=".firecrawl-ingest-ready-",
                delete=False,
            ) as probe:
                probe.write(b"research-store-ingest-ready")
                probe.flush()
                os.fsync(probe.fileno())
                probe_path = Path(probe.name)
            renamed_path = probe_path.with_suffix(".verified")
            os.replace(probe_path, renamed_path)
            probe_path = None
        except OSError as exc:
            raise SystemExit(f"blob root write probe failed: {exc}") from exc
        finally:
            for path in (probe_path, renamed_path):
                if path is not None:
                    path.unlink(missing_ok=True)
        print(dumps({"ready": True, "schema": schema, "blob_root": config.blob_root}))
        return 0
    if args.command == "import-scratch":
        root = Path(args.path) if args.path else config.scratch_root
        report = import_scratch(
            root, None if args.dry_run else build_service(config), args.dry_run
        )
        if args.report:
            export_json(Path(args.report), report)
        print(dumps(report))
        return 1 if report["failed"] else 0
    if args.command == "ingest-result":
        path = Path(args.file)
        result = build_service(config).ingest(
            IngestRequest(
                requested_url=args.url,
                content=path.read_bytes(),
                mime_type="application/json"
                if path.suffix == ".json"
                else "text/markdown",
                title=args.title,
                metadata=json.loads(args.metadata_json),
            )
        )
        print(dumps(result.__dict__))
        return 0
    if args.command == "verify-blobs":
        health = _blob_health(config)
        print(dumps(health))
        return 0 if health["ok"] else 1
    if args.command in {"worker", "index-once"}:
        worker = _worker(config)
        if args.command == "index-once":
            result = worker.run_forever(batch_size=args.limit, once=True)
        else:
            worker.lease_seconds = args.lease_seconds or config.job_lease_seconds
            worker.max_attempts = args.max_attempts or config.max_index_attempts
            result = worker.run_forever(
                batch_size=args.batch_size,
                poll_seconds=args.poll_seconds or config.worker_poll_seconds,
                once=args.once,
            )
        print(dumps(result))
        return 1 if result["failed"] else 0
    if args.command == "index-list":
        print(
            dumps(
                {
                    "alias": config.qdrant_alias,
                    "aliases": _qdrant(config).list_aliases(),
                    "definitions": _index_rows(config),
                }
            )
        )
        return 0
    if args.command in {"index-build", "reindex"}:
        print(dumps(_index_build(config, args.document)))
        return 0
    if args.command == "index-activate":
        print(dumps(_activate_index(config, args.id, "activate")))
        return 0
    if args.command == "index-rollback":
        print(dumps(_activate_index(config, args.id, "rollback")))
        return 0
    if args.command == "index-prune":
        if args.dry_run and args.force:
            raise SystemExit("--dry-run and --force are mutually exclusive")
        if args.force and not args.index_id:
            raise SystemExit("--force requires --index-id for an exact prune target")
        if args.keep_last < 0:
            raise SystemExit("--keep-last must be non-negative")
        aliases = _qdrant(config).list_aliases()
        active = aliases.get(config.qdrant_alias)
        rows = _index_rows(config)
        if args.index_id:
            rows = [row for row in rows if str(row["id"]) == args.index_id]
            if not rows:
                raise SystemExit("index definition not found")
        else:
            rows = rows[args.keep_last :]
        candidates = [row for row in rows if row["physical_collection"] != active]
        result = {
            "action": "deleted" if args.force else "dry_run",
            "indexes": [
                {"id": row["id"], "collection": row["physical_collection"]}
                for row in candidates
            ],
        }
        if args.force:
            for row in candidates:
                _qdrant(
                    config,
                    row["physical_collection"],
                    row["dimension"],
                    row["distance_metric"],
                ).delete_collection()
        print(dumps(result))
        return 0
    if args.command == "reconcile-qdrant":
        aliases = _qdrant(config).list_aliases()
        collection = aliases.get(config.qdrant_alias)
        if not collection:
            raise SystemExit(f"Qdrant alias is not configured: {config.qdrant_alias}")
        rows = [
            row
            for row in _index_rows(config)
            if row["physical_collection"] == collection
        ]
        if not rows:
            raise SystemExit("active collection has no PostgreSQL index definition")
        index = _qdrant(
            config, collection, rows[0]["dimension"], rows[0]["distance_metric"]
        )
        qdrant_ids, offset = set(), None
        while True:
            page = index.point_ids(offset, filters=_derivation_filter(config))
            qdrant_ids.update(str(item["id"]) for item in page.get("points", []))
            offset = page.get("next_page_offset")
            if not offset:
                break
        postgres_ids = {str(value) for value in _active_chunk_ids(config)}
        print(
            dumps(
                {
                    "collection": collection,
                    "orphaned_qdrant": sorted(qdrant_ids - postgres_ids),
                    "missing_qdrant": sorted(postgres_ids - qdrant_ids),
                }
            )
        )
        return 0 if qdrant_ids == postgres_ids else 1
    if args.command == "prune-cache":
        print(dumps({"deleted": ValkeyQueue(config.valkey_url).prune_cache()}))
        return 0
    if args.command == "rederive":
        service = build_service(config)
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT a.id,a.requested_url,a.final_url,a.retrieved_at,a.http_status,
                a.etag,a.last_modified,a.mime_type,a.content_sha256,a.firecrawl_version,
                a.crawl_options,d.title,d.published_at,d.metadata
                FROM asset_snapshots a LEFT JOIN LATERAL(
                  SELECT title,published_at,metadata FROM documents
                  WHERE snapshot_id=a.id ORDER BY id DESC LIMIT 1
                ) d ON true WHERE (%s::uuid IS NULL OR a.id=%s::uuid)
                ORDER BY a.retrieved_at,a.id""",
                (args.snapshot, args.snapshot),
            )
            snapshots = cur.fetchall()
        store = ContentAddressedBlobStore(config.blob_root)
        results = []
        for row in snapshots:
            with store.open(row[8]) as handle:
                content = handle.read()
            result = service.ingest(
                IngestRequest(
                    requested_url=row[1],
                    final_url=row[2],
                    retrieved_at=row[3],
                    http_status=row[4],
                    etag=row[5],
                    last_modified=row[6],
                    mime_type=row[7] or "text/markdown",
                    content=content,
                    firecrawl_version=row[9],
                    crawl_options=row[10] or {},
                    title=row[11],
                    published_at=row[12],
                    metadata=row[13] or {},
                )
            )
            results.append(result.__dict__)
        print(dumps({"rederived": len(results), "assets": results}))
        return 0
    if args.command == "export-invocation":
        with _uow_factory(config)() as uow:
            result = uow.export_invocation(args.invocation_id)
        export_json(Path(args.output), result)
        print(dumps(result))
        return 0
    if args.command == "export-run":
        with _db(config) as conn, conn.cursor() as cur:
            try:
                internal_id = UUID(args.id)
                cur.execute(
                    "SELECT row_to_json(r) FROM research_runs r WHERE id=%s",
                    (internal_id,),
                )
            except ValueError:
                cur.execute(
                    "SELECT row_to_json(r) FROM research_runs r WHERE external_run_id=%s",
                    (args.id,),
                )
            run = cur.fetchone()
            if not run:
                raise SystemExit("research run not found")
            internal_id = run[0]["id"]
            cur.execute(
                "SELECT row_to_json(e) FROM retrieval_events e WHERE run_id=%s ORDER BY created_at",
                (internal_id,),
            )
            events = [row[0] for row in cur.fetchall()]
        export_json(Path(args.output), {"run": run[0], "retrieval_events": events})
        return 0
    if args.command == "run-start":
        status = build_run_service(config).create(
            args.objective,
            args.external_id,
            execution_mode=args.mode,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            catalog_pointer=args.catalog_pointer,
            skill_version="research-store-v3",
        )
        print(dumps(status.to_dict()))
        return 0
    if args.command in {
        "run-status",
        "run-mode-change",
        "run-transition",
        "run-finish",
        "run-reopen",
        "run-cancel",
    }:
        run_service = build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        if args.command == "run-status":
            print(dumps(status.to_dict()))
            return 0
        expected_revision = (
            args.expected_revision
            if args.expected_revision is not None
            else status.lifecycle_revision
        )
    if args.command == "run-mode-change":
        result = run_service.change_execution_mode(
            status.id,
            args.mode,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            reason=args.reason,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-transition":
        result = run_service.transition(
            status.id,
            args.next_state,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
            semantic_proposal_id=(
                UUID(args.semantic_proposal_id) if args.semantic_proposal_id else None
            ),
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-finish":
        next_state = (
            "failed"
            if args.status == "failed"
            else "partial"
            if args.outcome == "partial"
            else "completed"
        )
        idempotency_key = args.idempotency_key or (
            f"run:finish:{args.status}:{args.outcome}:"
            f"{args.source_manifest_sha256 or ''}:{args.answer_sha256 or ''}"
        )
        result = run_service.transition(
            status.id,
            next_state,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=args.actor,
            outcome=args.outcome,
            error=args.outcome if next_state == "failed" else None,
            completion={
                "catalog_pointer": args.catalog_pointer,
                "source_manifest_sha256": args.source_manifest_sha256,
                "answer_sha256": args.answer_sha256,
            },
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-reopen":
        result = run_service.reopen(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:reopen:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-cancel":
        result = run_service.cancel(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:cancel:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "budget-record":
        from research_domain import load_model, serialize_model
        from research_domain.models import ResearchSpec

        spec_payload = json.loads(Path(args.research_spec).read_text(encoding="utf-8"))
        spec = load_model(spec_payload)
        if not isinstance(spec, ResearchSpec):
            raise SystemExit("--research-spec must contain research-spec-v1")
        snapshot = json.loads(Path(args.budget_snapshot).read_text(encoding="utf-8"))
        required = {
            "snapshot_version",
            "policy_version",
            "policy_config_sha256",
            "research_spec_id",
            "spec_revision",
            "run_revision",
            "effective_caps",
        }
        missing = sorted(required - set(snapshot))
        if missing:
            raise SystemExit(f"budget snapshot missing required fields: {missing}")
        if snapshot["research_spec_id"] != str(spec.research_spec_id):
            raise SystemExit("budget snapshot references another ResearchSpec")
        run_id = _resolve_run_id(config, args.external_id)
        with _uow_factory(config)() as uow:
            spec_id = uow.record_research_spec(
                run_id,
                snapshot["spec_revision"],
                "research-spec",
                1,
                serialize_model(spec),
                f"research-spec:{spec.research_spec_id}:r{snapshot['spec_revision']}",
            )
            budget_id = uow.record_budget_snapshot(
                run_id,
                spec_id,
                snapshot["spec_revision"],
                snapshot["run_revision"],
                snapshot["policy_version"],
                snapshot["policy_config_sha256"],
                snapshot,
                "budget:"
                f"{snapshot['policy_version']}:r{snapshot['run_revision']}:"
                f"{spec.research_spec_id}",
            )
        print(dumps({"id": budget_id, "external_run_id": args.external_id}))
        return 0
    if args.command == "legacy-comparisons":
        with _uow_factory(config)() as uow:
            rows = uow.runs.list_legacy_adapter_comparisons(
                external_run_id=args.research_run_id,
                external_invocation_id=args.invocation_id,
                entry_point=args.entry_point,
                divergent_only=args.divergent_only,
                limit=args.limit,
            )
        print(dumps({"comparisons": rows, "count": len(rows)}))
        return 0
    if args.command == "search-plan-record":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        with open(args.search_plan, "r", encoding="utf-8") as f:
            plan_payload = json.load(f)
        plan_id = run_svc.record_search_plan(
            status.id,
            UUID(args.research_spec_id),
            args.revision,
            plan_payload,
            args.idempotency_key,
        )
        print(dumps({"id": plan_id, "external_run_id": args.external_id}))
        return 0
    if args.command == "search-plan-get":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        plan_id = UUID(args.plan_id) if args.plan_id else None
        plan = run_svc.get_search_plan(status.id, plan_id=plan_id, revision=args.revision)
        print(dumps(plan))
        return 0
    if args.command == "search-plan-query-get":
        run_svc = build_run_service(config)
        query = run_svc.get_plan_query(UUID(args.query_id))
        print(dumps(query))
        return 0
    if args.command == "search-response-record":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        if args.payload_file:
            with open(args.payload_file, "rb") as f:
                raw_payload = f.read()
        else:
            raw_payload = sys.stdin.buffer.read()
        resp = run_svc.record_search_response(
            status.id,
            args.query_text,
            args.backend,
            raw_payload,
            args.idempotency_key,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            provider_request_id=args.provider_request_id,
            parser_version=args.parser_version,
            http_status=args.http_status,
        )
        print(dumps(resp))
        return 0
    if args.command == "search-response-get":
        run_svc = build_run_service(config)
        resp = run_svc.get_search_response(UUID(args.response_id))
        print(dumps(resp))
        return 0
    if args.command == "search-response-replay":
        run_svc = build_run_service(config)
        replay = run_svc.replay_search_response(UUID(args.response_id))
        out = {
            "id": str(replay.id),
            "run_id": str(replay.run_id),
            "query_text": replay.query_text,
            "backend": replay.backend,
            "status": replay.status,
            "parser_version": replay.parser_version,
            "raw_blob_sha256": replay.raw_blob_sha256,
            "content_sha256": replay.content_sha256,
            "raw_bytes_len": len(replay.raw_bytes),
            "integrity_verified": replay.verify_integrity(),
            "result_count": replay.result_count,
            "parsed_json": replay.parsed_json,
        }
        print(dumps(out))
        return 0
    if args.command == "candidate-record-response":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        occs = run_svc.record_response_candidates(
            status.id, UUID(args.search_response_id)
        )
        print(dumps(occs))
        return 0
    if args.command == "candidate-get":
        run_svc = build_run_service(config)
        cand = run_svc.get_candidate(UUID(args.candidate_id))
        print(dumps(cand))
        return 0
    if args.command == "candidate-list":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        cands = run_svc.list_candidates(
            status.id,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=UUID(args.duplicate_group_id) if args.duplicate_group_id else None,
        )
        print(dumps(cands))
        return 0
    if args.command == "candidate-occurrences-list":
        run_svc = build_run_service(config)
        occs = run_svc.list_candidate_occurrences(UUID(args.candidate_id))
        print(dumps(occs))
        return 0
    if args.command == "candidate-assign-group":
        run_svc = build_run_service(config)
        cand_ids = [UUID(cid) for cid in args.candidate_ids]
        group_id = UUID(args.group_id) if args.group_id else None
        res_group_id = run_svc.assign_duplicate_group(cand_ids, group_id=group_id)
        print(dumps({"group_id": res_group_id}))
        return 0




    service = build_service(config)
    if args.command == "corpus-overview":
        result = service.corpus_overview()
    elif args.command == "search-assets":
        filters = {
            key: value
            for key, value in {
                "domain": args.domain,
                "source_type": args.source_type,
                "date_from": args.date_from,
                "date_to": args.date_to,
            }.items()
            if value
        }
        result = service.search_assets(
            args.query,
            filters=filters,
            candidate_limit=args.limit,
            run_id=_resolve_run_id(config, args.research_run_id),
        )
    elif args.command == "inspect-asset":
        result = service.inspect_asset(UUID(args.id))
    elif args.command == "fetch-passages":
        ids = [UUID(value) for value in args.ids]
        result = service.fetch_passages(
            ids, max_tokens=args.max_tokens, max_passages=args.max_passages
        )
        run_id = _resolve_run_id(config, args.research_run_id)
        if run_id:
            with _uow_factory(config)() as uow:
                for rank, passage in enumerate(result, 1):
                    uow.log_retrieval(
                        run_id,
                        {
                            "stage": "passage_fetch",
                            "retriever": "explicit_selection",
                            "candidate_type": "chunk",
                            "candidate_id": passage["chunk_id"],
                            "rank": rank,
                            "selected": True,
                        },
                    )
    elif args.command == "expand-relationships":
        result = service.expand_relationships(
            [UUID(value) for value in args.ids],
            max_hops=args.max_hops,
            max_results=args.max_results,
            max_tokens=args.max_tokens,
        )
    elif args.command == "build-evidence-packet":
        result = service.build_evidence_packet(
            [UUID(value) for value in args.ids], max_tokens=args.max_tokens
        )
    else:
        raise AssertionError(args.command)
        print(dumps(result))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
