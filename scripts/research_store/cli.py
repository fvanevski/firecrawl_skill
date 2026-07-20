from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .compat import export_json, import_scratch
from .config import StoreConfig
from .container import build_service
from .domain import IngestRequest
from .indexing import IndexWorker, OpenAICompatibleEmbedder
from .postgres import connect
from .postgres import PostgresUnitOfWork
from .qdrant import QdrantIndex
from .queue import ValkeyQueue
from .retrieval import CohereCompatibleReranker
from .service import dumps


def parser():
    root = argparse.ArgumentParser(
        prog="research-db", description="Research asset store operations"
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")
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
    reindex = sub.add_parser("reindex")
    group = reindex.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--document")
    worker = sub.add_parser("index-once")
    worker.add_argument("--limit", type=int, default=64)
    sub.add_parser("reconcile-qdrant")
    sub.add_parser("prune-cache")
    export = sub.add_parser("export-run")
    export.add_argument("id")
    export.add_argument("--output", required=True)
    sub.add_parser("doctor")
    sub.add_parser("corpus-overview")
    search = sub.add_parser("search-assets")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--domain")
    search.add_argument("--source-type")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    inspect = sub.add_parser("inspect-asset")
    inspect.add_argument("id")
    fetch = sub.add_parser("fetch-passages")
    fetch.add_argument("ids", nargs="+")
    fetch.add_argument("--max-tokens", type=int, default=2000)
    fetch.add_argument("--max-passages", type=int, default=8)
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
        alembic_config = Config(str(ini))
        command.upgrade(alembic_config, "head")
        print(json.dumps({"schema_version": "0001_research_store"}))
        return 0
    if args.command == "status":
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT max(version) FROM schema_migrations")
            version = cur.fetchone()[0]
            cur.execute("SELECT status,count(*) FROM index_jobs GROUP BY status")
            jobs = dict(cur.fetchall())
        print(dumps({"schema_version": version, "index_jobs": jobs}))
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
        store = ContentAddressedBlobStore(config.blob_root)
        missing = []
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT id,content_sha256 FROM asset_snapshots")
            for snapshot_id, digest in cur.fetchall():
                if not store.verify(digest):
                    missing.append({"snapshot_id": str(snapshot_id), "sha256": digest})
        print(dumps({"checked": "all", "missing_or_corrupt": missing}))
        return 1 if missing else 0
    if args.command == "reindex":
        with _db(config) as conn, conn.cursor() as cur:
            if args.all:
                cur.execute(
                    """INSERT INTO index_jobs(entity_type,entity_id,index_name,operation,status)
                    SELECT 'chunk',id,%s,'upsert','pending' FROM chunks ON CONFLICT(entity_type,entity_id,index_name,operation)
                    DO UPDATE SET status='pending',available_at=now(),error=NULL""",
                    (config.qdrant_collection,),
                )
            else:
                cur.execute(
                    """INSERT INTO index_jobs(entity_type,entity_id,index_name,operation,status)
                    SELECT 'chunk',id,%s,'upsert','pending' FROM chunks WHERE document_id=%s
                    ON CONFLICT(entity_type,entity_id,index_name,operation) DO UPDATE SET status='pending',available_at=now(),error=NULL""",
                    (config.qdrant_collection, UUID(args.document)),
                )
            count = cur.rowcount
        print(dumps({"scheduled": count}))
        return 0
    if args.command == "index-once":
        if not config.embedding_url:
            raise RuntimeError("EMBEDDING_URL is required to process index jobs")
        from functools import partial

        factory = partial(
            PostgresUnitOfWork,
            config.database_url,
            config.qdrant_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
        )
        index = QdrantIndex(
            config.qdrant_url,
            config.qdrant_api_key,
            config.qdrant_collection,
            config.embedding_dimension,
        )
        embedder = OpenAICompatibleEmbedder(
            config.embedding_url,
            config.embedding_model,
            config.embedding_api_key,
            config.embedding_dimension,
        )
        print(dumps(IndexWorker(factory, index, embedder).run_batch(args.limit)))
        return 0
    if args.command == "reconcile-qdrant":
        index = QdrantIndex(
            config.qdrant_url,
            config.qdrant_api_key,
            config.qdrant_collection,
            config.embedding_dimension,
        )
        index.ensure_schema()
        qdrant_ids, offset = set(), None
        while True:
            page = index.point_ids(offset)
            qdrant_ids.update(UUID(str(item["id"])) for item in page.get("points", []))
            offset = page.get("next_page_offset")
            if not offset:
                break
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM chunks")
            postgres_ids = {row[0] for row in cur.fetchall()}
        print(
            dumps(
                {
                    "orphaned_qdrant": sorted(map(str, qdrant_ids - postgres_ids)),
                    "missing_qdrant": sorted(map(str, postgres_ids - qdrant_ids)),
                }
            )
        )
        return 0
    if args.command == "prune-cache":
        print(dumps({"deleted": ValkeyQueue(config.valkey_url).prune_cache()}))
        return 0
    if args.command == "export-run":
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT row_to_json(r) FROM research_runs r WHERE id=%s",
                (UUID(args.id),),
            )
            run = cur.fetchone()
            if not run:
                raise SystemExit("research run not found")
            cur.execute(
                "SELECT row_to_json(e) FROM retrieval_events e WHERE run_id=%s ORDER BY created_at",
                (UUID(args.id),),
            )
            events = [r[0] for r in cur.fetchall()]
        export_json(Path(args.output), {"run": run[0], "retrieval_events": events})
        return 0
    if args.command == "doctor":
        checks, failed = {}, False
        try:
            with _db(config) as conn, conn.cursor() as cur:
                cur.execute("SELECT max(version) FROM schema_migrations")
                checks["postgres"] = {"ok": True, "schema_version": cur.fetchone()[0]}
                cur.execute(
                    "SELECT status,count(*) FROM index_jobs WHERE status IN ('pending','failed','dead') GROUP BY status"
                )
                checks["index_jobs"] = dict(cur.fetchall())
        except Exception as exc:
            checks["postgres"] = {"ok": False, "error": str(exc)}
            failed = True
        try:
            config.blob_root.mkdir(parents=True, exist_ok=True)
            probe = config.blob_root / ".doctor"
            probe.touch()
            probe.unlink()
            checks["blob_root"] = {"ok": True}
        except Exception as exc:
            checks["blob_root"] = {"ok": False, "error": str(exc)}
            failed = True
        try:
            QdrantIndex(
                config.qdrant_url,
                config.qdrant_api_key,
                config.qdrant_collection,
                config.embedding_dimension,
            ).ensure_schema()
            checks["qdrant"] = {"ok": True}
        except Exception as exc:
            checks["qdrant"] = {"ok": False, "error": str(exc)}
            failed = True
        try:
            import redis

            checks["valkey"] = {
                "ok": bool(redis.Redis.from_url(config.valkey_url).ping())
            }
        except Exception as exc:
            checks["valkey"] = {"ok": False, "error": str(exc)}
            failed = True
        try:
            if not config.embedding_url:
                raise RuntimeError("EMBEDDING_URL is not configured")
            vector = OpenAICompatibleEmbedder(
                config.embedding_url,
                config.embedding_model,
                config.embedding_api_key,
                config.embedding_dimension,
            )("research-store-doctor")
            checks["embedding_endpoint"] = {
                "ok": True,
                "model": config.embedding_model,
                "dimension": len(vector),
            }
        except Exception as exc:
            checks["embedding_endpoint"] = {"ok": False, "error": str(exc)}
            failed = True
        try:
            if not config.reranker_url:
                raise RuntimeError("RERANKER_URL is not configured")
            reranked = CohereCompatibleReranker(
                config.reranker_url, config.reranker_model, config.reranker_api_key
            )(
                "research database",
                [
                    {
                        "candidate_id": "relevant",
                        "excerpt": "PostgreSQL research database",
                    },
                    {"candidate_id": "irrelevant", "excerpt": "yellow bananas"},
                ],
            )
            if not reranked or reranked[0]["candidate_id"] != "relevant":
                raise RuntimeError("reranker probe returned an unexpected order")
            checks["reranker_endpoint"] = {
                "ok": True,
                "model": config.reranker_model,
            }
        except Exception as exc:
            checks["reranker_endpoint"] = {"ok": False, "error": str(exc)}
            failed = True
        checks["versions"] = {
            "embedding_model": config.embedding_model,
            "embedding_revision": config.embedding_revision,
            "dimension": config.embedding_dimension,
            "parser": config.parser_version,
            "chunker": config.chunker_version,
        }
        print(dumps(checks))
        return 1 if failed else 0
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
        )
    elif args.command == "inspect-asset":
        result = service.inspect_asset(UUID(args.id))
    elif args.command == "fetch-passages":
        result = service.fetch_passages(
            [UUID(v) for v in args.ids],
            max_tokens=args.max_tokens,
            max_passages=args.max_passages,
        )
    elif args.command == "expand-relationships":
        result = service.expand_relationships(
            [UUID(v) for v in args.ids],
            max_hops=args.max_hops,
            max_results=args.max_results,
            max_tokens=args.max_tokens,
        )
    elif args.command == "build-evidence-packet":
        result = service.build_evidence_packet(
            [UUID(v) for v in args.ids], max_tokens=args.max_tokens
        )
    else:
        raise AssertionError(args.command)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
