from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .domain import BlobReference, IngestRequest, IngestResult


def connect(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg 3 (pip install 'psycopg[binary]')"
        ) from exc
    return psycopg.connect(database_url)


def require_disposable_database_reset(
    database_url: str, acknowledgement: str
) -> str:
    """Reject destructive test setup unless two independent guards agree."""
    database_name = unquote(urlsplit(database_url).path.rsplit("/", 1)[-1])
    test_segments = database_name.replace("-", "_").replace(".", "_").split("_")
    if "test" not in test_segments:
        raise RuntimeError(
            "refusing destructive integration reset: database name must contain "
            "a standalone 'test' segment"
        )
    if acknowledgement != database_name:
        raise RuntimeError(
            "refusing destructive integration reset: "
            "RESEARCH_STORE_TEST_ALLOW_RESET must equal the exact database name"
        )
    return database_name


def migrate(database_url: str, revision: str = "head") -> int:
    """Upgrade with Alembic, the sole migration authority."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise RuntimeError("migrations require Alembic") from exc

    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        revision = cursor.fetchone()[0]
    return int(revision[:4])


class PostgresUnitOfWork:
    """One repository facade and one explicit transaction boundary."""

    def __init__(
        self,
        database_url: str,
        index_name: str,
        embedding_model: str = "",
        embedding_revision: str = "",
        embedding_dimension: int = 1,
        parser_version: str = "markdown-v1",
        normalization_version: str = "cleanup-v1",
        chunker_version: str = "structural-v1",
    ):
        self.database_url = database_url
        self.index_name = index_name
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_dimension = embedding_dimension
        self.parser_version = parser_version
        self.normalization_version = normalization_version
        self.chunker_version = chunker_version
        self.connection = None

    def __enter__(self):
        self.connection = connect(self.database_url)
        self.sources = self.snapshots = self.documents = self.chunks = self.runs = (
            self.retrieval_events
        ) = self.index_jobs = self
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.rollback() if exc else self.commit()
        finally:
            self.connection.close()
        return False

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def savepoint(self):
        """Return a nested transaction context managed as a PostgreSQL savepoint."""
        return self.connection.transaction()

    def persist_ingest(
        self,
        request: IngestRequest,
        canonical_url: str,
        blob: BlobReference,
        normalized_text: str,
        blocks,
        chunks,
        parser_version: str,
        chunker_version: str,
        normalization_version: str,
    ) -> IngestResult:
        domain = urlsplit(canonical_url).hostname
        document_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
        with self.connection.cursor() as cur:
            # The conflict update takes a row lock.  All snapshot decisions for
            # one canonical source are therefore serialized before the unique
            # (source_id, content_sha256) key is consulted.
            cur.execute(
                """INSERT INTO sources(canonical_url, registered_domain, metadata)
                VALUES (%s,%s,%s) ON CONFLICT(canonical_url) DO UPDATE SET last_seen_at=now(), metadata=sources.metadata || excluded.metadata
                RETURNING id""",
                (canonical_url, domain, json.dumps(request.metadata)),
            )
            source_id = cur.fetchone()[0]
            cur.execute(
                "SELECT id FROM asset_snapshots WHERE source_id=%s AND content_sha256=%s",
                (source_id, blob.sha256),
            )
            existing = cur.fetchone()
            reused_snapshot = existing is not None
            if existing:
                snapshot_id = existing[0]
            else:
                cur.execute(
                    """SELECT id FROM asset_snapshots WHERE source_id=%s
                    ORDER BY retrieved_at DESC, id DESC LIMIT 1""",
                    (source_id,),
                )
                prior = cur.fetchone()
                cur.execute(
                    """INSERT INTO asset_snapshots(source_id,requested_url,final_url,retrieved_at,http_status,etag,last_modified,mime_type,
                    content_sha256,raw_blob_uri,raw_byte_length,firecrawl_version,crawl_options,parent_snapshot_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        source_id,
                        request.requested_url,
                        request.final_url,
                        request.retrieved_at,
                        request.http_status,
                        request.etag,
                        request.last_modified,
                        request.mime_type,
                        blob.sha256,
                        blob.uri,
                        blob.byte_length,
                        request.firecrawl_version,
                        json.dumps(request.crawl_options),
                        prior[0] if prior else None,
                    ),
                )
                snapshot_id = cur.fetchone()[0]

            cur.execute(
                """SELECT id FROM documents
                WHERE snapshot_id=%s AND parser_name='markdown' AND parser_version=%s
                  AND normalization_version=%s AND document_sha256=%s""",
                (
                    snapshot_id,
                    parser_version,
                    normalization_version,
                    document_hash,
                ),
            )
            row = cur.fetchone()
            reused_document = row is not None
            if row:
                document_id = row[0]
                cur.execute(
                    "SELECT id,ordinal FROM document_blocks WHERE document_id=%s",
                    (document_id,),
                )
                block_ids = {ordinal: block_id for block_id, ordinal in cur.fetchall()}
            else:
                cur.execute(
                    """INSERT INTO documents(snapshot_id,title,published_at,normalized_markdown,normalized_text,parser_name,
                    parser_version,normalization_version,document_sha256,metadata)
                    VALUES(%s,%s,%s,%s,%s,'markdown',%s,%s,%s,%s) RETURNING id""",
                    (
                        snapshot_id,
                        request.title,
                        request.published_at,
                        normalized_text,
                        normalized_text,
                        parser_version,
                        normalization_version,
                        document_hash,
                        json.dumps(request.metadata),
                    ),
                )
                document_id = cur.fetchone()[0]
                block_ids = {}
                for block in blocks:
                    cur.execute(
                        """INSERT INTO document_blocks(document_id,block_type,heading_path,ordinal,char_start,char_end,text,metadata)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (
                            document_id,
                            block.block_type,
                            list(block.heading_path),
                            block.ordinal,
                            block.char_start,
                            block.char_end,
                            block.text,
                            json.dumps(block.metadata),
                        ),
                    )
                    block_ids[block.ordinal] = cur.fetchone()[0]

            cur.execute(
                """SELECT id FROM chunks WHERE document_id=%s
                AND chunker_name='structural' AND chunker_version=%s ORDER BY ordinal""",
                (document_id, chunker_version),
            )
            chunk_ids = [row[0] for row in cur.fetchall()]
            reused_chunks = bool(chunk_ids)
            if not chunk_ids:
                for chunk in chunks:
                    cur.execute(
                        """INSERT INTO chunks(document_id,first_block_id,last_block_id,ordinal,text,token_count,content_sha256,
                        chunker_name,chunker_version,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,'structural',%s,%s) RETURNING id""",
                        (
                            document_id,
                            block_ids[chunk.first_block_ordinal],
                            block_ids[chunk.last_block_ordinal],
                            chunk.ordinal,
                            chunk.text,
                            chunk.token_count,
                            chunk.content_sha256,
                            chunker_version,
                            json.dumps({"heading_path": list(chunk.heading_path)}),
                        ),
                    )
                    chunk_ids.append(cur.fetchone()[0])

            definition = self._ensure_index_definition(cur)
            for chunk_id in chunk_ids:
                cur.execute(
                    """INSERT INTO embedding_manifests(
                    chunk_id,model_name,model_revision,dimension,distance_metric,
                    normalization,instruction_template_hash,qdrant_collection,
                    qdrant_point_id,index_status,index_definition_id)
                    VALUES(%s,%s,%s,%s,'Cosine','unit-length','',%s,%s,'pending',%s)
                    ON CONFLICT(chunk_id,index_definition_id) DO UPDATE
                    SET qdrant_collection=excluded.qdrant_collection
                    RETURNING id""",
                    (
                        chunk_id,
                        self.embedding_model,
                        self.embedding_revision,
                        self.embedding_dimension,
                        definition["physical_collection"],
                        chunk_id,
                        definition["id"],
                    ),
                )
                manifest_id = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO index_jobs(
                    entity_type,entity_id,index_name,operation,status,manifest_id,index_definition_id)
                    VALUES('chunk',%s,%s,'upsert','pending',%s,%s)
                    ON CONFLICT(manifest_id,operation) DO NOTHING""",
                    (
                        chunk_id,
                        definition["physical_collection"],
                        manifest_id,
                        definition["id"],
                    ),
                )
            return IngestResult(
                source_id,
                snapshot_id,
                document_id,
                tuple(chunk_ids),
                blob.sha256,
                reused_snapshot,
                reused_document,
                reused_chunks,
            )

    def _ensure_index_definition(self, cur):
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "model": self.embedding_model,
                    "revision": self.embedding_revision,
                    "dimension": self.embedding_dimension,
                    "distance": "Cosine",
                    "normalization": "unit-length",
                    "instruction_template_hash": "",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        physical = f"research_chunks_{fingerprint[:12]}"
        cur.execute(
            """INSERT INTO index_definitions(
            fingerprint,physical_collection,model_name,model_revision,dimension,
            distance_metric,normalization,instruction_template_hash)
            VALUES(%s,%s,%s,%s,%s,'Cosine','unit-length','')
            ON CONFLICT(fingerprint) DO UPDATE SET fingerprint=excluded.fingerprint
            RETURNING id,fingerprint,physical_collection,model_name,model_revision,
              dimension,distance_metric,normalization,instruction_template_hash,lifecycle_status""",
            (
                fingerprint,
                physical,
                self.embedding_model,
                self.embedding_revision,
                self.embedding_dimension,
            ),
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
        )
        return dict(zip(keys, cur.fetchone()))

    def ensure_index_definition(self):
        with self.connection.cursor() as cur:
            return self._ensure_index_definition(cur)

    def corpus_overview(self):
        with self.connection.cursor() as cur:
            cur.execute("""SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM asset_snapshots),
                (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks),
                (SELECT min(retrieved_at) FROM asset_snapshots), (SELECT max(retrieved_at) FROM asset_snapshots)""")
            row = cur.fetchone()
            cur.execute(
                "SELECT registered_domain,count(*) FROM sources GROUP BY registered_domain ORDER BY count(*) DESC LIMIT 50"
            )
            overview = {
                "sources": row[0],
                "snapshots": row[1],
                "documents": row[2],
                "chunks": row[3],
                "retrieved_range": [row[4], row[5]],
                "domains": dict(cur.fetchall()),
            }
            cur.execute("SELECT source_type,count(*) FROM sources GROUP BY source_type")
            overview["source_types"] = dict(cur.fetchall())
            cur.execute(
                "SELECT qdrant_collection,model_name,model_revision,dimension,count(*) FROM embedding_manifests GROUP BY 1,2,3,4"
            )
            overview["indexes"] = [
                {
                    "collection": r[0],
                    "model": r[1],
                    "revision": r[2],
                    "dimension": r[3],
                    "chunks": r[4],
                }
                for r in cur.fetchall()
            ]
            cur.execute("SELECT status,count(*) FROM research_runs GROUP BY status")
            overview["research_runs"] = dict(cur.fetchall())
            return overview

    def search_lexical(self, query: str, limit: int, filters: dict):
        domain = filters.get("domain")
        source_type = filters.get("source_type")
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,d.title,s.registered_domain,d.published_at,c.metadata->'heading_path',
                left(c.text,400),ts_rank_cd(c.search_vector,websearch_to_tsquery('simple',%s)) score,
                a.id,s.id,s.canonical_url,a.retrieved_at
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id WHERE c.search_vector @@ websearch_to_tsquery('simple',%s)
                AND d.parser_version=%s AND d.normalization_version=%s
                AND c.chunker_version=%s
                AND (%s::text IS NULL OR s.registered_domain=%s::text)
                AND (%s::text IS NULL OR s.source_type=%s::text)
                AND (%s::timestamptz IS NULL OR coalesce(d.published_at,a.retrieved_at) >= %s::timestamptz)
                AND (%s::timestamptz IS NULL OR coalesce(d.published_at,a.retrieved_at) <= %s::timestamptz)
                ORDER BY score DESC LIMIT %s""",
                (
                    query,
                    query,
                    self.parser_version,
                    self.normalization_version,
                    self.chunker_version,
                    domain,
                    domain,
                    source_type,
                    source_type,
                    date_from,
                    date_from,
                    date_to,
                    date_to,
                    limit,
                ),
            )
            keys = (
                "candidate_id",
                "title",
                "domain",
                "date",
                "heading_path",
                "excerpt",
                "lexical_score",
                "snapshot_id",
                "source_id",
                "url",
                "retrieved_at",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def inspect_asset(self, candidate_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.document_id,d.snapshot_id,a.source_id,s.canonical_url,a.retrieved_at,d.title,
                c.ordinal,c.metadata->'heading_path',c.token_count,a.content_sha256,a.parent_snapshot_id
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id WHERE c.id=%s""",
                (candidate_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(str(candidate_id))
            keys = (
                "candidate_id",
                "document_id",
                "snapshot_id",
                "source_id",
                "url",
                "retrieved_at",
                "title",
                "ordinal",
                "heading_path",
                "token_count",
                "content_sha256",
                "parent_snapshot_id",
            )
            result = dict(zip(keys, row))
            cur.execute(
                "SELECT ordinal,block_type,heading_path,left(text,160) FROM document_blocks WHERE document_id=%s ORDER BY ordinal",
                (row[1],),
            )
            result["outline"] = [
                {"ordinal": r[0], "type": r[1], "heading_path": r[2], "preview": r[3]}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT id,retrieved_at,content_sha256,parent_snapshot_id FROM asset_snapshots WHERE source_id=%s ORDER BY retrieved_at",
                (row[3],),
            )
            result["version_history"] = [
                {
                    "snapshot_id": r[0],
                    "retrieved_at": r[1],
                    "content_sha256": r[2],
                    "parent_snapshot_id": r[3],
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT id,ordinal,metadata->'heading_path' FROM chunks WHERE document_id=%s AND ordinal BETWEEN %s AND %s ORDER BY ordinal",
                (row[1], max(0, row[7] - 1), row[7] + 1),
            )
            result["neighboring_candidates"] = [
                {"candidate_id": r[0], "ordinal": r[1], "heading_path": r[2]}
                for r in cur.fetchall()
            ]
            return result

    def fetch_passages(
        self, candidate_ids, max_tokens, max_passages, include_neighbors
    ):
        if not candidate_ids or max_tokens <= 0 or max_passages <= 0:
            return []
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.document_id,c.ordinal,c.text,c.token_count,c.metadata->'heading_path',d.snapshot_id,
                a.source_id,s.canonical_url,a.retrieved_at FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id JOIN sources s ON s.id=a.source_id
                WHERE c.id=ANY(%s) ORDER BY array_position(%s::uuid[],c.id)""",
                (candidate_ids, candidate_ids),
            )
            passages, used = [], 0
            for row in cur.fetchall():
                if len(passages) >= max_passages or used + row[4] > max_tokens:
                    break
                keys = (
                    "chunk_id",
                    "document_id",
                    "ordinal",
                    "text",
                    "token_count",
                    "heading_path",
                    "snapshot_id",
                    "source_id",
                    "url",
                    "retrieved_at",
                )
                passages.append(dict(zip(keys, row)))
                used += row[4]
            return passages

    def expand_relationships(self, candidate_ids, hops, max_results):
        with self.connection.cursor() as cur:
            cur.execute(
                """WITH RECURSIVE walk AS (
                SELECT r.*,1 depth FROM relations r WHERE r.subject_id=ANY(%s)
                UNION ALL SELECT r.*,w.depth+1 FROM relations r JOIN walk w ON r.subject_id=w.object_id
                WHERE w.depth < %s) SELECT id,subject_type,subject_id,predicate,object_type,object_id,object_literal,
                relation_class,source_snapshot_id,source_block_id,supporting_span,confidence,depth FROM walk LIMIT %s""",
                (candidate_ids, hops, max_results),
            )
            keys = (
                "id",
                "subject_type",
                "subject_id",
                "predicate",
                "object_type",
                "object_id",
                "object_literal",
                "relation_class",
                "source_snapshot_id",
                "source_block_id",
                "supporting_span",
                "confidence",
                "depth",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def start_run(self, original_request, metadata):
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs(original_request,query_plan,skill_version,llm_model,
                retrieval_policy_version,status,external_run_id,catalog_pointer)
                VALUES(%s,%s,%s,%s,%s,'running',%s,%s)
                ON CONFLICT(external_run_id) DO UPDATE
                SET external_run_id=excluded.external_run_id
                RETURNING id""",
                (
                    original_request,
                    json.dumps(metadata.get("query_plan")),
                    metadata.get("skill_version"),
                    metadata.get("llm_model"),
                    metadata.get("policy_version"),
                    metadata.get("external_run_id"),
                    metadata.get("catalog_pointer"),
                ),
            )
            return cur.fetchone()[0]

    def link_run_asset(self, external_run_id, snapshot_id, role="acquired", metadata=None):
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                SELECT id,%s,%s,%s FROM research_runs
                WHERE external_run_id=%s AND status='running'
                ON CONFLICT(run_id,snapshot_id,role) DO UPDATE
                SET metadata=research_run_assets.metadata || excluded.metadata""",
                (snapshot_id, role, json.dumps(metadata or {}), external_run_id),
            )
            if cur.rowcount != 1:
                raise KeyError(external_run_id)

    def start_ingestion_batch(
        self, invocation_id, operation, research_run_external_id=None, metadata=None
    ):
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (invocation_id,),
            )
            research_run_id = None
            if research_run_external_id is not None:
                cur.execute(
                    """SELECT id,status FROM research_runs
                    WHERE external_run_id=%s FOR SHARE""",
                    (research_run_external_id,),
                )
                run = cur.fetchone()
                if run is None:
                    raise KeyError(research_run_external_id)
                if run[1] != "running":
                    raise ValueError(
                        "ingestion batches require a running research run"
                    )
                research_run_id = run[0]
            cur.execute(
                """SELECT b.id,b.operation,r.external_run_id,b.status
                FROM ingestion_batches b
                LEFT JOIN research_runs r ON r.id=b.research_run_id
                WHERE b.invocation_id=%s FOR UPDATE OF b""",
                (invocation_id,),
            )
            existing = cur.fetchone()
            if existing:
                if existing[1] != operation or existing[2] != research_run_external_id:
                    raise ValueError(
                        "invocation ID reuse requires the original operation and research run"
                    )
                if existing[3] == "running":
                    raise ValueError(
                        "invocation ID is already running; retry only after it is terminal"
                    )
                batch_id = existing[0]
                cur.execute(
                    """UPDATE ingestion_batches SET status='running',completed_at=NULL,
                    error=NULL,metadata=metadata || %s WHERE id=%s""",
                    (json.dumps(metadata or {}), batch_id),
                )
            else:
                cur.execute(
                    """INSERT INTO ingestion_batches(
                    invocation_id,operation,research_run_id,metadata)
                    VALUES(%s,%s,%s,%s)
                    RETURNING id""",
                    (
                        invocation_id,
                        operation,
                        research_run_id,
                        json.dumps(metadata or {}),
                    ),
                )
                batch_id = cur.fetchone()[0]
            # Invocation retries replace the reconstructable result ledger.
            # The delete is in the same outer transaction, so a catastrophic
            # retry rollback restores the prior complete ledger.
            cur.execute(
                "DELETE FROM ingestion_batch_assets WHERE batch_id=%s", (batch_id,)
            )
            return batch_id

    def record_batch_asset(
        self, batch_id, ordinal, requested_url, status, result=None, error=None, metadata=None
    ):
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO ingestion_batch_assets(
                batch_id,ordinal,requested_url,status,source_id,snapshot_id,document_id,chunk_ids,error,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(batch_id,ordinal) DO UPDATE SET
                  requested_url=excluded.requested_url,status=excluded.status,
                  source_id=excluded.source_id,snapshot_id=excluded.snapshot_id,
                  document_id=excluded.document_id,chunk_ids=excluded.chunk_ids,
                  error=excluded.error,metadata=excluded.metadata""",
                (
                    batch_id,
                    ordinal,
                    requested_url,
                    status,
                    result.source_id if result else None,
                    result.snapshot_id if result else None,
                    result.document_id if result else None,
                    list(result.chunk_ids) if result else [],
                    error,
                    json.dumps(metadata or {}),
                ),
            )

    def finish_ingestion_batch(self, batch_id, status, error=None):
        with self.connection.cursor() as cur:
            cur.execute(
                """UPDATE ingestion_batches SET status=%s,error=%s,completed_at=now()
                WHERE id=%s""",
                (status, error, batch_id),
            )

    def export_invocation(self, invocation_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT b.id,b.invocation_id,b.operation,b.status,b.started_at,b.completed_at,
                b.error,b.metadata,r.external_run_id
                FROM ingestion_batches b LEFT JOIN research_runs r ON r.id=b.research_run_id
                WHERE b.invocation_id=%s""",
                (invocation_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(invocation_id)
            keys = ("batch_id","invocation_id","operation","status","started_at","completed_at","error","metadata","research_run_id")
            result = dict(zip(keys, row))
            cur.execute(
                """SELECT ordinal,requested_url,status,source_id,snapshot_id,document_id,chunk_ids,error,metadata
                FROM ingestion_batch_assets WHERE batch_id=%s ORDER BY ordinal""",
                (row[0],),
            )
            asset_keys = ("ordinal","requested_url","status","source_id","snapshot_id","document_id","chunk_ids","error","metadata")
            result["assets"] = [dict(zip(asset_keys, asset)) for asset in cur.fetchall()]
            return result

    def log_retrieval(self, run_id, event):
        fields = (
            "stage",
            "query",
            "filters",
            "retriever",
            "candidate_type",
            "candidate_id",
            "raw_score",
            "normalized_score",
            "rank",
            "reranker_score",
            "selected",
            "rejection_reason",
        )
        values = [
            json.dumps(event.get(f)) if f == "filters" else event.get(f) for f in fields
        ]
        with self.connection.cursor() as cur:
            cur.execute(
                f"""INSERT INTO retrieval_events(run_id,{','.join(fields)})
                SELECT id,{','.join(['%s'] * len(fields))}
                FROM research_runs WHERE id=%s AND status='running'""",
                [*values, run_id],
            )
            if cur.rowcount != 1:
                raise KeyError(f"research run is absent or finished: {run_id}")

    def claim_jobs(
        self,
        limit,
        lease_seconds=300,
        worker_id="compat",
        max_attempts=5,
        fingerprint=None,
    ):
        if limit <= 0 or lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("job limits and lease duration must be positive")
        with self.connection.cursor() as cur:
            cur.execute(
                """UPDATE index_jobs SET status='dead',
                error='lease expired after final allowed attempt',
                lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE attempt_count >= %s AND (
                  (status='running' AND lease_expires_at < now())
                  OR status IN ('pending','failed'))
                RETURNING manifest_id,error""",
                (max_attempts,),
            )
            exhausted = cur.fetchall()
            if exhausted:
                cur.executemany(
                    """UPDATE embedding_manifests SET index_status='failed',error=%s
                    WHERE id=%s""",
                    [(error, manifest_id) for manifest_id, error in exhausted],
                )
            cur.execute(
                """WITH claimed AS (
                SELECT id FROM index_jobs
                WHERE ((status IN ('pending','failed') AND available_at <= now())
                    OR (status='running' AND lease_expires_at < now()))
                  AND attempt_count < %s
                  AND (%s::text IS NULL OR EXISTS(
                    SELECT 1 FROM index_definitions d
                    WHERE d.id=index_jobs.index_definition_id AND d.fingerprint=%s))
                ORDER BY coalesce(lease_expires_at,available_at),created_at
                FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE index_jobs j SET status='running',started_at=coalesce(started_at,now()),
                  attempt_count=attempt_count+1,error=NULL,lease_token=gen_random_uuid(),
                  lease_owner=%s,lease_expires_at=now() + make_interval(secs => %s),updated_at=now()
                FROM claimed, embedding_manifests em, index_definitions d
                WHERE j.id=claimed.id AND em.id=j.manifest_id AND d.id=j.index_definition_id
                RETURNING j.id,j.manifest_id,j.index_definition_id,j.entity_id,j.operation,
                  j.attempt_count,j.lease_token,d.fingerprint,d.physical_collection,
                  d.model_name,d.model_revision,d.dimension,d.distance_metric,
                  d.normalization,d.instruction_template_hash""",
                (max_attempts, fingerprint, fingerprint, limit, worker_id, lease_seconds),
            )
            keys = (
                "id", "manifest_id", "index_definition_id", "entity_id", "operation",
                "attempt_count", "lease_token", "fingerprint", "physical_collection",
                "model_name", "model_revision", "dimension", "distance_metric",
                "normalization", "instruction_template_hash",
            )
            return [
                {**dict(zip(keys, r)), "chunk_id": r[3]}
                for r in cur.fetchall()
            ]

    def renew_job(self, job_id, lease_token, lease_seconds=300):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.connection.cursor() as cur:
            cur.execute(
                """UPDATE index_jobs SET lease_expires_at=now() + make_interval(secs => %s),updated_at=now()
                WHERE id=%s AND status='running' AND lease_token=%s AND lease_expires_at >= now()""",
                (lease_seconds, job_id, lease_token),
            )
            return cur.rowcount == 1

    def finish_job(self, job_id, lease_token, error=None, max_attempts=5):
        if not isinstance(lease_token, UUID):
            raise TypeError("finish_job requires the UUID lease token returned by claim_jobs")
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT manifest_id,attempt_count FROM index_jobs
                WHERE id=%s AND status='running' AND lease_token=%s FOR UPDATE""",
                (job_id, lease_token),
            )
            row = cur.fetchone()
            if not row:
                return False
            manifest_id, attempt_count = row
            if error:
                status = "dead" if attempt_count >= max_attempts else "failed"
                cur.execute(
                    """UPDATE index_jobs SET status=%s,
                    available_at=now() + make_interval(secs => least(3600,power(2,attempt_count)::int)),
                    error=%s,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                    WHERE id=%s""",
                    (status, error, job_id),
                )
                cur.execute(
                    "UPDATE embedding_manifests SET index_status='failed',error=%s WHERE id=%s",
                    (error, manifest_id),
                )
            else:
                cur.execute(
                    """UPDATE index_jobs SET status='complete',completed_at=now(),error=NULL,
                    lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (job_id,),
                )
                cur.execute(
                    "UPDATE embedding_manifests SET index_status='complete',indexed_at=now(),error=NULL WHERE id=%s",
                    (manifest_id,),
                )
            return True

    def heartbeat_worker(self, worker_id, metadata=None):
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO index_worker_heartbeats(worker_id,metadata) VALUES(%s,%s)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=now(),metadata=excluded.metadata""",
                (worker_id, json.dumps(metadata or {})),
            )
            cur.execute(
                "DELETE FROM index_worker_heartbeats WHERE heartbeat_at < now()-interval '7 days'"
            )

    def worker_status(self):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT worker_id,heartbeat_at,metadata FROM index_worker_heartbeats
                ORDER BY heartbeat_at DESC LIMIT 20"""
            )
            workers = [
                {"worker_id": r[0], "heartbeat_at": r[1], "metadata": r[2]}
                for r in cur.fetchall()
            ]
            cur.execute(
                """SELECT count(*) FILTER(WHERE status='running' AND lease_expires_at < now()),
                min(available_at) FILTER(WHERE status IN ('pending','failed')),
                count(*) FILTER(WHERE status='dead'),
                count(*) FILTER(WHERE status='running' AND lease_expires_at >= now())
                FROM index_jobs"""
            )
            stale, oldest, dead, active = cur.fetchone()
            return {
                "workers": workers,
                "stale_leases": stale,
                "oldest_pending": oldest,
                "dead_jobs": dead,
                "active_leases": active,
            }

    def chunks_for_index(self, chunk_ids=None, manifest_id=None):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT c.id chunk_id,c.text,c.document_id,d.snapshot_id,a.source_id,s.registered_domain,s.source_type,s.canonical_url,
                d.title,c.metadata->'heading_path' heading_path,a.retrieved_at,d.published_at,d.language,c.content_sha256,
                s.default_authority_class,d.parser_version,d.normalization_version,c.chunker_version
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id
                WHERE (%s::uuid[] IS NULL OR c.id=ANY(%s))
                  AND (%s::uuid IS NULL OR EXISTS(
                    SELECT 1 FROM embedding_manifests em WHERE em.chunk_id=c.id AND em.id=%s::uuid))""",
                (chunk_ids, chunk_ids, manifest_id, manifest_id),
            )
            keys = (
                "chunk_id",
                "text",
                "document_id",
                "snapshot_id",
                "source_id",
                "domain",
                "source_type",
                "url",
                "title",
                "heading_path",
                "retrieved_at",
                "published_at",
                "language",
                "content_sha256",
                "authority_class",
                "parser_version",
                "normalization_version",
                "chunker_version",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]
