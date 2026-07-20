from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .domain import BlobReference, IngestRequest, IngestResult


def connect(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg 3 (pip install 'psycopg[binary]')"
        ) from exc
    return psycopg.connect(database_url)


def migrate(database_url: str) -> int:
    migration_dir = Path(__file__).with_name("migrations")
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cursor.fetchall()}
            for path in sorted(migration_dir.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version not in applied:
                    cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()
    return max(
        [
            0,
            *applied,
            *[int(p.name.split("_", 1)[0]) for p in migration_dir.glob("*.sql")],
        ]
    )


class PostgresUnitOfWork:
    """One repository facade and one explicit transaction boundary."""

    def __init__(
        self,
        database_url: str,
        index_name: str,
        embedding_model: str = "",
        embedding_revision: str = "",
        embedding_dimension: int = 1,
    ):
        self.database_url = database_url
        self.index_name = index_name
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_dimension = embedding_dimension
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
    ) -> IngestResult:
        domain = urlsplit(canonical_url).hostname
        with self.connection.cursor() as cur:
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
            if existing:
                snapshot_id = existing[0]
                cur.execute(
                    "SELECT id FROM documents WHERE snapshot_id=%s", (snapshot_id,)
                )
                document_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT id FROM chunks WHERE document_id=%s ORDER BY ordinal",
                    (document_id,),
                )
                return IngestResult(
                    source_id,
                    snapshot_id,
                    document_id,
                    tuple(row[0] for row in cur.fetchall()),
                    blob.sha256,
                    True,
                )
            cur.execute(
                "SELECT id FROM asset_snapshots WHERE source_id=%s ORDER BY retrieved_at DESC LIMIT 1",
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
            document_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
            cur.execute(
                """INSERT INTO documents(snapshot_id,title,published_at,normalized_markdown,normalized_text,parser_name,
                parser_version,normalization_version,document_sha256,metadata)
                VALUES(%s,%s,%s,%s,%s,'markdown',%s,'v1',%s,%s) RETURNING id""",
                (
                    snapshot_id,
                    request.title,
                    request.published_at,
                    normalized_text,
                    normalized_text,
                    parser_version,
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
            chunk_ids = []
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
                chunk_id = cur.fetchone()[0]
                chunk_ids.append(chunk_id)
                cur.execute(
                    """INSERT INTO embedding_manifests(chunk_id,model_name,model_revision,dimension,distance_metric,
                    qdrant_collection,qdrant_point_id,index_status) VALUES(%s,%s,%s,%s,'Cosine',%s,%s,'pending')
                    ON CONFLICT DO NOTHING""",
                    (
                        chunk_id,
                        self.embedding_model,
                        self.embedding_revision,
                        self.embedding_dimension,
                        self.index_name,
                        chunk_id,
                    ),
                )
                cur.execute(
                    """INSERT INTO index_jobs(entity_type,entity_id,index_name,operation,status)
                    VALUES('chunk',%s,%s,'upsert','pending') ON CONFLICT DO NOTHING""",
                    (chunk_id, self.index_name),
                )
            return IngestResult(
                source_id,
                snapshot_id,
                document_id,
                tuple(chunk_ids),
                blob.sha256,
                False,
            )

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
                AND (%s::text IS NULL OR s.registered_domain=%s::text)
                AND (%s::text IS NULL OR s.source_type=%s::text)
                AND (%s::timestamptz IS NULL OR coalesce(d.published_at,a.retrieved_at) >= %s::timestamptz)
                AND (%s::timestamptz IS NULL OR coalesce(d.published_at,a.retrieved_at) <= %s::timestamptz)
                ORDER BY score DESC LIMIT %s""",
                (
                    query,
                    query,
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
                "INSERT INTO research_runs(original_request,query_plan,skill_version,llm_model,retrieval_policy_version,status) VALUES(%s,%s,%s,%s,%s,'running') RETURNING id",
                (
                    original_request,
                    json.dumps(metadata.get("query_plan")),
                    metadata.get("skill_version"),
                    metadata.get("llm_model"),
                    metadata.get("policy_version"),
                ),
            )
            return cur.fetchone()[0]

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
                f"INSERT INTO retrieval_events(run_id,{','.join(fields)}) VALUES(%s,{','.join(['%s'] * len(fields))})",
                [run_id, *values],
            )

    def claim_jobs(self, limit):
        with self.connection.cursor() as cur:
            cur.execute(
                """WITH claimed AS (
                SELECT id FROM index_jobs WHERE status IN ('pending','failed') AND available_at <= now()
                AND attempt_count < 5 ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE index_jobs j SET status='running',started_at=now(),attempt_count=attempt_count+1,error=NULL
                FROM claimed WHERE j.id=claimed.id RETURNING j.id,j.entity_id,j.operation,j.attempt_count""",
                (limit,),
            )
            return [
                {
                    "id": r[0],
                    "entity_id": r[1],
                    "operation": r[2],
                    "attempt_count": r[3],
                }
                for r in cur.fetchall()
            ]

    def finish_job(self, job_id, error=None):
        with self.connection.cursor() as cur:
            cur.execute("SELECT entity_id FROM index_jobs WHERE id=%s", (job_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(str(job_id))
            chunk_id = row[0]
            if error:
                cur.execute(
                    """UPDATE index_jobs SET status=CASE WHEN attempt_count >= 5 THEN 'dead' ELSE 'failed' END,
                    available_at=now() + make_interval(secs => least(3600,power(2,attempt_count)::int)),error=%s WHERE id=%s""",
                    (error, job_id),
                )
                cur.execute(
                    "UPDATE embedding_manifests SET index_status='failed',error=%s WHERE chunk_id=%s AND qdrant_collection=%s",
                    (error, chunk_id, self.index_name),
                )
            else:
                cur.execute(
                    "UPDATE index_jobs SET status='complete',completed_at=now(),error=NULL WHERE id=%s",
                    (job_id,),
                )
                cur.execute(
                    "UPDATE embedding_manifests SET index_status='complete',indexed_at=now(),error=NULL WHERE chunk_id=%s AND qdrant_collection=%s",
                    (chunk_id, self.index_name),
                )

    def chunks_for_index(self, chunk_ids=None):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT c.id chunk_id,c.text,c.document_id,d.snapshot_id,a.source_id,s.registered_domain,s.source_type,s.canonical_url,
                d.title,c.metadata->'heading_path' heading_path,a.retrieved_at,d.published_at,d.language,c.content_sha256,
                s.default_authority_class,d.parser_version,c.chunker_version
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id WHERE (%s::uuid[] IS NULL OR c.id=ANY(%s))""",
                (chunk_ids, chunk_ids),
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
                "chunker_version",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]
