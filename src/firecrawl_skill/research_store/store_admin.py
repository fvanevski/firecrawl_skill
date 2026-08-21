from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from firecrawl_skill.research_store.retrieval.projection.indexing import (
    OpenAICompatibleEmbedder,
)

from .blob import ContentAddressedBlobStore
from .domain import IngestRequest
from .retrieval import CohereCompatibleReranker
from .store_runtime import database


def schema_state(config) -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).parents[3] / "alembic.ini"
    head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    with database(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        current = row[0] if row else None
    return {"current": current, "head": head, "at_head": current == head}


def migrate(config) -> dict[str, Any]:
    config.require_database()
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise RuntimeError(
            "migrations require dependencies from requirements-research-store.txt"
        ) from exc
    ini = Path(__file__).parents[3] / "alembic.ini"
    command.upgrade(Config(str(ini)), "head")
    return schema_state(config)


def status(config) -> tuple[dict[str, Any], int]:
    schema = schema_state(config)
    with database(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,count(*) FROM index_jobs GROUP BY status")
        jobs = dict(cur.fetchall())
        if schema["at_head"]:
            cur.execute("SELECT status,count(*) FROM ingestion_batches GROUP BY status")
            batches = dict(cur.fetchall())
        else:
            batches = {"available": False, "reason": "migration required"}
    return {"schema": schema, "index_jobs": jobs, "batches": batches}, (
        0 if schema["at_head"] else 1
    )


def ingest_ready(config) -> dict[str, Any]:
    schema = schema_state(config)
    if not schema["at_head"]:
        raise SystemExit(
            f"research store migration required: {schema['current']} != {schema['head']}"
        )
    if not config.blob_root.is_dir():
        raise SystemExit(f"blob root is not writable: {config.blob_root}")
    with database(config) as conn, conn.cursor() as cur:
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
            "ingestion_batch_assets": ("SELECT", "INSERT", "UPDATE", "DELETE"),
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
    return {"ready": True, "schema": schema, "blob_root": config.blob_root}


def parser_info(config) -> dict[str, Any]:
    from .parsing import get_registry

    registry = get_registry()
    return {
        "parser_registry_version": config.parser_registry_version,
        "parser_version": config.parser_version,
        "normalization_version": config.normalization_version,
        "chunker_version": config.chunker_version,
        "registered_parsers": registry.list_registered(),
    }


def ingest_result(config, args, build_service):
    path = Path(args.file)
    return (
        build_service(config)
        .ingest(
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
        .__dict__
    )


def blob_health(config, *, database_fn=database) -> dict[str, Any]:
    store = ContentAddressedBlobStore(config.blob_root)
    with database_fn(config) as conn, conn.cursor() as cur:
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
    unreferenced = sorted(disk_hashes - references.keys())
    return {
        "integrity": "pass" if not missing else "failure",
        "referenced": len(references),
        "missing_or_corrupt": missing,
        "unreferenced_inventory": unreferenced,
        "orphan_count": len(unreferenced),
    }


def classify_connectivity_failure(exc: BaseException) -> dict[str, str]:
    """Legacy research-db diagnostic classification contract."""
    message = str(exc).lower()
    if any(
        token in message
        for token in (
            "permission denied",
            "operation not permitted",
            "errno1",
            "errno13",
        )
    ):
        return {
            "status": "failure",
            "reason_code": "network_policy_denial",
            "detail": str(exc),
        }
    if any(
        token in message
        for token in ("connection refused", "connect ECONNREFUSED", "errno111")
    ):
        return {
            "status": "failure",
            "reason_code": "server_unavailable",
            "detail": str(exc),
        }
    if any(
        token in message
        for token in (
            "authentication",
            "password",
            "credential",
            "auth",
            "forbidden",
            "errno13",
            "access denied",
        )
    ):
        return {
            "status": "failure",
            "reason_code": "credential_failure",
            "detail": str(exc),
        }
    if any(
        token in message
        for token in (
            "no route to host",
            "network unreachable",
            "errno101",
            "timeout",
            "timed out",
        )
    ):
        return {
            "status": "failure",
            "reason_code": "network_namespace_denial",
            "detail": str(exc),
        }
    if any(
        token in message
        for token in ("database", "pg_", "psycopg", "sqlalchemy", "dialect", "adapter")
    ):
        return {
            "status": "failure",
            "reason_code": "database_rejection",
            "detail": str(exc),
        }
    return {
        "status": "failure",
        "reason_code": "query_runtime_failure",
        "detail": str(exc),
    }


def doctor(config, deps) -> tuple[dict[str, Any], bool]:
    """Preserve the direct ``research_store.cli.main(['doctor'])`` contract."""
    from datetime import datetime, timezone

    checks: dict[str, Any] = {}
    failed = False
    try:
        checks["schema"] = deps._schema_state(config)
        if not checks["schema"]["at_head"]:
            failed = True
        with deps._db(config) as conn, conn.cursor() as cur:
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
            with deps._uow_factory(config)() as uow:
                checks["worker"] = uow.index_jobs.worker_status()
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
    except Exception as exc:  # noqa: BLE001
        checks["postgres_authority"] = deps._classify_connectivity_failure(exc)
        failed = True

    try:
        if not config.blob_root.is_dir():
            raise RuntimeError(f"blob root is not a directory: {config.blob_root}")
        if not os.access(config.blob_root, os.R_OK | os.X_OK):
            raise RuntimeError("blob root is not readable")
        checks["referenced_blob_integrity"] = deps._blob_health(config)
        if checks["referenced_blob_integrity"]["integrity"] == "failure":
            failed = True
    except Exception as exc:  # noqa: BLE001
        checks["referenced_blob_integrity"] = deps._classify_connectivity_failure(exc)
        failed = True

    try:
        alias_state = deps._qdrant_alias_state(config)
        qdrant = {
            "status": "pass",
            "alias": config.qdrant_alias,
            "collection": alias_state["actual_required_alias_target"],
            "alias_state": alias_state,
        }
        if alias_state["status"] != "healthy":
            qdrant["status"] = "failure"
            failed = True
            checks["qdrant_projection"] = qdrant
        else:
            active = alias_state["actual_required_alias_target"]
            if active and not checks.get("schema", {}).get("at_head"):
                qdrant["schema"] = deps._qdrant(config, active).inspect_schema()
                checks["qdrant_projection"] = qdrant
                active = None
            if active:
                rows = [
                    row
                    for row in deps._index_rows(config)
                    if row["physical_collection"] == active
                ]
                if not rows:
                    raise RuntimeError(
                        "active alias is not backed by an index definition"
                    )
                row = rows[0]
                qdrant["query_embedding_compatible"] = (
                    row["fingerprint"] == config.embedding_fingerprint
                )
                if not qdrant["query_embedding_compatible"]:
                    qdrant["status"] = "failure"
                    failed = True
                qdrant["schema"] = deps._qdrant(
                    config, active, row["dimension"], row["distance_metric"]
                ).inspect_schema()
                if not qdrant["schema"]["compatible"]:
                    qdrant["status"] = "failure"
                    failed = True
                if checks.get("schema", {}).get("at_head"):
                    point_ids: set[str] = set()
                    offset = None
                    active_index = deps._qdrant(
                        config, active, row["dimension"], row["distance_metric"]
                    )
                    while True:
                        page = active_index.point_ids(
                            offset, filters=deps._derivation_filter(config)
                        )
                        point_ids.update(
                            str(item["id"]) for item in page.get("points", [])
                        )
                        offset = page.get("next_page_offset")
                        if not offset:
                            break
                    chunk_ids = {str(value) for value in deps._active_chunk_ids(config)}
                    qdrant["coverage"] = {
                        "missing": len(chunk_ids - point_ids),
                        "orphaned": len(point_ids - chunk_ids),
                    }
                    if row["lifecycle_status"] == "active" and len(point_ids) == 0:
                        qdrant["status"] = "failure"
                        qdrant["drift"] = {
                            "type": "empty_active_projection",
                            "message": (
                                f"PostgreSQL reports index definition {row['id']} as active "
                                f"with complete jobs/manifests but Qdrant collection {active} "
                                "has zero points. This indicates cross-store activation drift "
                                "between PostgreSQL and Qdrant."
                            ),
                        }
                        failed = True
                    elif point_ids != chunk_ids:
                        qdrant["status"] = "failure"
                        failed = True
                    else:
                        qdrant["status"] = "pass"
                else:
                    qdrant["status"] = "inconclusive"
            checks["qdrant_projection"] = qdrant
    except Exception as exc:  # noqa: BLE001
        checks["qdrant_projection"] = deps._classify_connectivity_failure(exc)
        failed = True

    try:
        if checks.get("schema", {}).get("at_head"):
            reconcile = deps._index_reconcile(config, repair=False)
            checks["index_job_health"] = {
                "status": "pass" if reconcile["ok"] else "failure",
                "total_active_chunks": reconcile["total_active_chunks"],
                "definitions": len(reconcile["definitions"]),
                "discrepancies": reconcile["discrepancies"],
            }
            if not reconcile["ok"]:
                failed = True
        else:
            checks["index_job_health"] = {
                "status": "inconclusive",
                "reason": "migration required",
            }
    except Exception as exc:  # noqa: BLE001
        checks["index_job_health"] = deps._classify_connectivity_failure(exc)
        failed = True

    try:
        import redis

        checks["environment_connectivity"] = {
            "status": "pass"
            if bool(redis.Redis.from_url(config.valkey_url).ping())
            else "failure",
            "component": "valkey",
        }
    except Exception as exc:  # noqa: BLE001
        checks["environment_connectivity"] = deps._classify_connectivity_failure(exc)
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
                checks[name] = {"status": "pass", "dimension": len(vector)}
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
                checks[name] = {"status": "pass"}
        except Exception as exc:  # noqa: BLE001
            checks[name] = deps._classify_connectivity_failure(exc)
            failed = True
    checks["configuration"] = {
        "embedding_fingerprint": config.embedding_fingerprint,
        "physical_collection": config.physical_collection,
        "normalization_version": config.normalization_version,
        "parser_version": config.parser_version,
        "chunker_version": config.chunker_version,
    }
    return checks, failed
