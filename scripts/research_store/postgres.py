from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .domain import (
    BlobReference,
    IngestRequest,
    IngestResult,
    utcnow,
)
from .parsing import parse_raw_search_response
from .url import canonicalize_candidate_url


try:
    from research_domain import load_model, serialize_model
    from research_domain.codec import to_dict
    from research_domain.models import SearchPlan
    from research_domain.validation import ValidationContext, validate_references
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1]))
    from research_domain import load_model, serialize_model
    from research_domain.codec import to_dict
    from research_domain.models import SearchPlan
    from research_domain.validation import ValidationContext, validate_references


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def connect(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg 3 (pip install 'psycopg[binary]')"
        ) from exc
    return psycopg.connect(database_url)


def require_disposable_database_reset(database_url: str, acknowledgement: str) -> str:
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
        ) = self.index_jobs = self.search_responses = self.candidates = (
            self.strategy_revisions
        ) = self.coverage = self.terminal_decisions = self

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
                retrieval_policy_version,status,external_run_id,catalog_pointer,state,
                execution_mode,objective,budget_policy_version,metadata)
                VALUES(%s,%s,%s,%s,%s,'running',%s,%s,'created',%s,%s,%s,%s)
                ON CONFLICT(external_run_id) DO NOTHING
                RETURNING id""",
                (
                    original_request,
                    json.dumps(metadata.get("query_plan")),
                    metadata.get("skill_version"),
                    metadata.get("llm_model"),
                    metadata.get("policy_version"),
                    metadata.get("external_run_id"),
                    metadata.get("catalog_pointer"),
                    metadata.get("execution_mode", "agent_led"),
                    original_request,
                    metadata.get("budget_policy_version"),
                    _canonical_json(metadata.get("metadata", {})),
                ),
            )
            inserted = cur.fetchone()
            if inserted is not None:
                return inserted[0]
            cur.execute(
                """SELECT id,objective,execution_mode FROM research_runs
                WHERE external_run_id=%s FOR UPDATE""",
                (metadata.get("external_run_id"),),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("research run conflict could not be resolved")
            if existing[1:] != (
                original_request,
                metadata.get("execution_mode", "agent_led"),
            ):
                raise ValueError("external run ID was used for another run")
            return existing[0]

    def get_run_status(self, *, run_id=None, external_id=None):
        if (run_id is None) == (external_id is None):
            raise ValueError("provide exactly one of run_id or external_id")
        with self.connection.cursor() as cur:
            columns = """id,external_run_id,state,lifecycle_revision,
                reopened_from_revision,execution_mode,objective,declared_outcome,
                status,completed_at,error"""
            if run_id is not None:
                cur.execute(
                    f"SELECT {columns} FROM research_runs WHERE id=%s", (run_id,)
                )
            else:
                cur.execute(
                    f"SELECT {columns} FROM research_runs WHERE external_run_id=%s",
                    (external_id,),
                )
            row = cur.fetchone()
        if row is None:
            raise KeyError(run_id or external_id)
        keys = (
            "id",
            "external_id",
            "state",
            "lifecycle_revision",
            "reopened_from_revision",
            "execution_mode",
            "objective",
            "declared_outcome",
            "legacy_status",
            "completed_at",
            "error",
        )
        return dict(zip(keys, row))

    @staticmethod
    def _lock_workflow_run(cur, run_id):
        cur.execute(
            """SELECT state,lifecycle_revision FROM research_runs
            WHERE id=%s FOR UPDATE""",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def append_run_transition(
        self,
        run_id,
        lifecycle_revision,
        prior_state,
        next_state,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        actor_identifier=None,
        triggering_event_id=None,
        semantic_proposal_id=None,
        validation_result=None,
        error=None,
    ):
        """Append one immutable ledger row without applying state-machine policy."""
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            validation_json = _canonical_json(validation_result or {})
            cur.execute(
                """SELECT id,lifecycle_revision,prior_state,next_state,
                triggering_event_id,actor_type,actor_identifier,policy_version,
                semantic_proposal_id,validation_result,error
                FROM research_run_transitions
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    lifecycle_revision,
                    prior_state,
                    next_state,
                    triggering_event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    json.loads(validation_json),
                    error,
                )
                if existing[1:] != expected:
                    raise ValueError("idempotency key was used for another transition")
                return {
                    "id": existing[0],
                    "lifecycle_revision": existing[1],
                    "prior_state": existing[2],
                    "next_state": existing[3],
                    "reused": True,
                }
            cur.execute(
                """INSERT INTO research_run_transitions(
                run_id,lifecycle_revision,prior_state,next_state,triggering_event_id,
                actor_type,actor_identifier,policy_version,semantic_proposal_id,
                validation_result,idempotency_key,error)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    run_id,
                    lifecycle_revision,
                    prior_state,
                    next_state,
                    triggering_event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    validation_json,
                    idempotency_key,
                    error,
                ),
            )
            transition_id = cur.fetchone()[0]
            return {
                "id": transition_id,
                "lifecycle_revision": lifecycle_revision,
                "prior_state": prior_state,
                "next_state": next_state,
                "reused": False,
            }

    def apply_run_transition(
        self,
        run_id,
        next_state,
        expected_revision,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        permitted_prior_states,
        actor_identifier=None,
        semantic_proposal_id=None,
        event_type,
        reason=None,
        outcome=None,
        error=None,
        completion=None,
        reopen=False,
    ):
        """Atomically lock, validate, record, and apply one lifecycle command."""
        completion = completion or {}
        command = {
            "expected_revision": expected_revision,
            "reason": reason,
            "outcome": outcome,
            "completion": completion,
            "reopen": reopen,
        }
        event_payload = {
            "next_state": next_state,
            "expected_revision": expected_revision,
            "reason": reason,
            "outcome": outcome,
            "policy_version": policy_version,
        }
        with self.connection.cursor() as cur:
            prior_state, current_revision = self._lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT t.id,t.triggering_event_id,t.lifecycle_revision,
                t.prior_state,t.next_state,t.actor_type,t.actor_identifier,
                t.policy_version,t.semantic_proposal_id,t.validation_result,t.error,
                e.event_type,e.payload
                FROM research_run_transitions t
                JOIN research_events e ON e.id=t.triggering_event_id
                WHERE t.run_id=%s AND t.idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    next_state,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    command,
                    error,
                    event_type,
                    event_payload | {"prior_state": existing[3]},
                )
                if existing[4:] != expected:
                    raise ValueError("idempotency key was used for another run command")
                return {
                    "transition_id": existing[0],
                    "event_id": existing[1],
                    "lifecycle_revision": existing[2],
                    "prior_state": existing[3],
                    "next_state": existing[4],
                    "reused": True,
                }
            if current_revision != expected_revision:
                raise ValueError(
                    "stale research run revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if prior_state not in permitted_prior_states:
                raise ValueError(
                    "research run transition rejected: "
                    f"{prior_state} -> {next_state} is not permitted"
                )
            if semantic_proposal_id is not None:
                cur.execute(
                    """SELECT validation_status,payload FROM semantic_artifacts
                    WHERE id=%s AND run_id=%s""",
                    (semantic_proposal_id, run_id),
                )
                proposal = cur.fetchone()
                if (
                    proposal is None
                    or proposal[0] != "valid"
                    or proposal[1].get("run_revision") != expected_revision
                ):
                    raise ValueError(
                        "semantic proposal is missing, cross-run, or stale"
                    )
            next_revision = current_revision + 1
            event_payload["prior_state"] = prior_state
            cur.execute(
                """INSERT INTO research_events(
                run_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    run_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    _canonical_json(event_payload),
                    next_revision,
                    idempotency_key,
                ),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO research_run_transitions(
                run_id,lifecycle_revision,prior_state,next_state,triggering_event_id,
                actor_type,actor_identifier,policy_version,semantic_proposal_id,
                validation_result,idempotency_key,error)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    next_revision,
                    prior_state,
                    next_state,
                    event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    _canonical_json(command),
                    idempotency_key,
                    error,
                ),
            )
            transition_id = cur.fetchone()[0]
            terminal = next_state in {"completed", "partial", "failed", "cancelled"}
            legacy_status = (
                "failed"
                if next_state in {"failed", "cancelled"}
                else "complete"
                if terminal
                else "running"
            )
            declared_outcome = outcome or {
                "completed": "satisfied",
                "partial": "partial",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(next_state)
            cur.execute(
                """UPDATE research_runs SET state=%s,lifecycle_revision=%s,
                reopened_from_revision=CASE WHEN %s THEN %s ELSE reopened_from_revision END,
                status=%s,declared_outcome=%s,outcome=%s,
                completed_at=CASE WHEN %s THEN now() ELSE NULL END,
                error=%s,
                catalog_pointer=coalesce(%s,catalog_pointer),
                source_manifest_sha256=CASE WHEN %s THEN %s ELSE source_manifest_sha256 END,
                answer_sha256=CASE WHEN %s THEN %s ELSE answer_sha256 END
                WHERE id=%s""",
                (
                    next_state,
                    next_revision,
                    reopen,
                    current_revision,
                    legacy_status,
                    declared_outcome,
                    declared_outcome,
                    terminal,
                    error,
                    completion.get("catalog_pointer"),
                    terminal,
                    completion.get("source_manifest_sha256"),
                    terminal,
                    completion.get("answer_sha256"),
                    run_id,
                ),
            )
            if reopen:
                cur.execute(
                    """UPDATE semantic_artifacts
                    SET validation_status='invalid',
                    validation_errors=validation_errors || %s::jsonb
                    WHERE run_id=%s AND validation_status='valid'""",
                    (
                        _canonical_json(
                            [
                                {
                                    "code": "stale_after_reopen",
                                    "invalidated_by_revision": next_revision,
                                    "reason": reason,
                                }
                            ]
                        ),
                        run_id,
                    ),
                )
                cur.execute(
                    """UPDATE research_runs SET source_manifest_sha256=NULL,
                    answer_sha256=NULL WHERE id=%s""",
                    (run_id,),
                )
            return {
                "transition_id": transition_id,
                "event_id": event_id,
                "lifecycle_revision": next_revision,
                "prior_state": prior_state,
                "next_state": next_state,
                "reused": False,
            }

    def revise_execution_mode(
        self,
        run_id,
        next_mode,
        expected_revision,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        requested_by,
        approved_by,
        reason,
        actor_identifier=None,
    ):
        """Atomically revise semantic authority and append its approval event."""
        with self.connection.cursor() as cur:
            state, current_revision = self._lock_workflow_run(cur, run_id)
            cur.execute(
                "SELECT execution_mode FROM research_runs WHERE id=%s", (run_id,)
            )
            current_mode = cur.fetchone()[0]
            cur.execute(
                """SELECT id,run_revision,event_type,actor_type,actor_identifier,payload
                FROM research_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected_payload = {
                    "prior_mode": existing[5].get("prior_mode"),
                    "next_mode": next_mode,
                    "expected_revision": expected_revision,
                    "requested_by": requested_by,
                    "approved_by": approved_by,
                    "reason": reason,
                    "policy_version": policy_version,
                    "semantic_artifacts_invalidated": True,
                }
                if existing[2:] != (
                    "run.execution_mode_changed",
                    actor_type,
                    actor_identifier,
                    expected_payload,
                ):
                    raise ValueError("idempotency key was used for another mode change")
                return {
                    "event_id": existing[0],
                    "lifecycle_revision": existing[1],
                    "prior_mode": existing[5]["prior_mode"],
                    "next_mode": existing[5]["next_mode"],
                    "reused": True,
                }
            if current_revision != expected_revision:
                raise ValueError(
                    "stale research run revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if state in {"completed", "partial", "failed", "cancelled"}:
                raise ValueError(
                    "research run mode change rejected: terminal runs must be reopened"
                )
            if current_mode == next_mode:
                raise ValueError(
                    "research run mode change rejected: next mode equals current mode"
                )
            next_revision = current_revision + 1
            payload = {
                "prior_mode": current_mode,
                "next_mode": next_mode,
                "expected_revision": expected_revision,
                "requested_by": requested_by,
                "approved_by": approved_by,
                "reason": reason,
                "policy_version": policy_version,
                "semantic_artifacts_invalidated": True,
            }
            cur.execute(
                """INSERT INTO research_events(
                run_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key)
                VALUES(%s,'run.execution_mode_changed',%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    actor_type,
                    actor_identifier,
                    _canonical_json(payload),
                    next_revision,
                    idempotency_key,
                ),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """UPDATE research_runs SET execution_mode=%s,lifecycle_revision=%s
                WHERE id=%s""",
                (next_mode, next_revision, run_id),
            )
            cur.execute(
                """UPDATE semantic_artifacts
                SET validation_status='invalid',
                validation_errors=validation_errors || %s::jsonb
                WHERE run_id=%s AND validation_status='valid'""",
                (
                    _canonical_json(
                        [
                            {
                                "code": "stale_after_mode_change",
                                "invalidated_by_revision": next_revision,
                                "prior_mode": current_mode,
                                "next_mode": next_mode,
                            }
                        ]
                    ),
                    run_id,
                ),
            )
            return {
                "event_id": event_id,
                "prior_mode": current_mode,
                "next_mode": next_mode,
                "lifecycle_revision": next_revision,
                "reused": False,
            }

    def record_invocation(
        self,
        run_id,
        operation,
        idempotency_key,
        *,
        parent_invocation_id=None,
        external_invocation_id=None,
        status="pending",
        input_payload=None,
        metadata=None,
    ):
        with self.connection.cursor() as cur:
            _state, revision = self._lock_workflow_run(cur, run_id)
            input_json = _canonical_json(input_payload or {})
            metadata_json = _canonical_json(metadata or {})
            cur.execute(
                """SELECT id,parent_invocation_id,external_invocation_id,operation,
                status,input,metadata FROM research_invocations
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    status,
                    json.loads(input_json),
                    json.loads(metadata_json),
                )
                if existing[1:] != expected:
                    raise ValueError("idempotency key was used for another invocation")
                return existing[0]
            cur.execute(
                """INSERT INTO research_invocations(
                run_id,parent_invocation_id,external_invocation_id,operation,status,
                lifecycle_revision,idempotency_key,input,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    status,
                    revision,
                    idempotency_key,
                    input_json,
                    metadata_json,
                ),
            )
            return cur.fetchone()[0]

    def append_event(
        self,
        run_id,
        event_type,
        actor_type,
        idempotency_key,
        *,
        invocation_id=None,
        actor_identifier=None,
        payload=None,
    ):
        with self.connection.cursor() as cur:
            _state, revision = self._lock_workflow_run(cur, run_id)
            payload_json = _canonical_json(payload or {})
            cur.execute(
                """SELECT id,invocation_id,event_type,actor_type,actor_identifier,payload
                FROM research_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    invocation_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    json.loads(payload_json),
                )
                if existing[1:] != expected:
                    raise ValueError("idempotency key was used for another event")
                return existing[0]
            cur.execute(
                """INSERT INTO research_events(
                run_id,invocation_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id,event_type,run_revision""",
                (
                    run_id,
                    invocation_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    payload_json,
                    revision,
                    idempotency_key,
                ),
            )
            event_id, stored_type, stored_revision = cur.fetchone()
            if (stored_type, stored_revision) != (event_type, revision):
                raise ValueError("idempotency key was used for another event")
            return event_id

    def record_research_spec(
        self,
        run_id,
        spec_revision,
        schema_name,
        schema_version,
        payload,
        idempotency_key,
        *,
        validation_status="valid",
        validation_errors=None,
    ):
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            digest = _json_sha256(payload)
            cur.execute(
                """INSERT INTO research_specs(
                run_id,spec_revision,schema_name,schema_version,payload,content_sha256,
                validation_status,validation_errors,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,spec_revision,content_sha256""",
                (
                    run_id,
                    spec_revision,
                    schema_name,
                    schema_version,
                    _canonical_json(payload),
                    digest,
                    validation_status,
                    _canonical_json(validation_errors or []),
                    idempotency_key,
                ),
            )
            spec_id, stored_revision, stored_digest = cur.fetchone()
            if (stored_revision, stored_digest) != (spec_revision, digest):
                raise ValueError("idempotency key was used for another research spec")
            cur.execute(
                "UPDATE research_runs SET research_spec_id=%s WHERE id=%s",
                (spec_id, run_id),
            )
            return spec_id

    def record_budget_snapshot(
        self,
        run_id,
        research_spec_id,
        spec_revision,
        run_revision,
        policy_version,
        policy_config_sha256,
        snapshot,
        idempotency_key,
    ):
        """Persist one immutable budget authorization boundary for a run."""
        expected_snapshot_fields = {
            "policy_version": policy_version,
            "policy_config_sha256": policy_config_sha256,
            "spec_revision": spec_revision,
            "run_revision": run_revision,
        }
        mismatched = {
            name: {"expected": expected, "actual": snapshot.get(name)}
            for name, expected in expected_snapshot_fields.items()
            if snapshot.get(name) != expected
        }
        if mismatched:
            raise ValueError(
                f"budget snapshot envelope does not match repository arguments: {mismatched}"
            )
        with self.connection.cursor() as cur:
            _, current_revision = self._lock_workflow_run(cur, run_id)
            if run_revision != current_revision:
                raise ValueError(
                    f"budget snapshot run revision {run_revision} is stale; "
                    f"current revision is {current_revision}"
                )
            cur.execute(
                """SELECT spec_revision FROM research_specs
                WHERE id=%s AND run_id=%s""",
                (research_spec_id, run_id),
            )
            stored_spec = cur.fetchone()
            if stored_spec is None:
                raise ValueError("budget snapshot references an unknown research spec")
            if stored_spec[0] != spec_revision:
                raise ValueError(
                    "budget snapshot spec revision does not match its spec"
                )
            payload_json = _canonical_json(snapshot)
            digest = _json_sha256(snapshot)
            cur.execute(
                """SELECT id,research_spec_id,spec_revision,run_revision,
                policy_version,policy_config_sha256,content_sha256
                FROM research_budget_snapshots
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            idempotent = cur.fetchone()
            expected = (
                research_spec_id,
                spec_revision,
                run_revision,
                policy_version,
                policy_config_sha256,
                digest,
            )
            if idempotent is not None:
                if idempotent[1:] != expected:
                    raise ValueError(
                        "idempotency key was used for another budget snapshot"
                    )
                return idempotent[0]
            cur.execute(
                """SELECT id,research_spec_id,spec_revision,policy_config_sha256,
                content_sha256 FROM research_budget_snapshots
                WHERE run_id=%s AND policy_version=%s AND run_revision=%s""",
                (run_id, policy_version, run_revision),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    research_spec_id,
                    spec_revision,
                    policy_config_sha256,
                    digest,
                )
                if existing[1:] != expected:
                    raise ValueError(
                        "budget change requires a new policy version or explicit run revision"
                    )
                return existing[0]
            cur.execute(
                """INSERT INTO research_budget_snapshots(
                run_id,research_spec_id,spec_revision,run_revision,policy_version,
                policy_config_sha256,snapshot,content_sha256,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,research_spec_id,spec_revision,run_revision,policy_version,
                policy_config_sha256,content_sha256""",
                (
                    run_id,
                    research_spec_id,
                    spec_revision,
                    run_revision,
                    policy_version,
                    policy_config_sha256,
                    payload_json,
                    digest,
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            expected = (
                research_spec_id,
                spec_revision,
                run_revision,
                policy_version,
                policy_config_sha256,
                digest,
            )
            if row[1:] != expected:
                raise ValueError("idempotency key was used for another budget snapshot")
            cur.execute(
                """UPDATE research_runs SET budget_snapshot_id=%s,
                budget_policy_version=%s WHERE id=%s""",
                (row[0], policy_version, run_id),
            )
            return row[0]

    def record_search_plan(
        self,
        run_id,
        research_spec_id,
        revision,
        search_plan,
        idempotency_key,
        **metadata,
    ):
        """Persist one versioned search plan and its queries transactionally."""
        if revision <= 0:
            raise ValueError("search plan revision must be positive")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")

        if isinstance(search_plan, dict):
            plan_payload = dict(search_plan)
            plan_model = load_model(plan_payload)
        else:
            plan_model = search_plan
            plan_payload = serialize_model(plan_model)

        if not isinstance(plan_model, SearchPlan):
            raise ValueError("provided payload is not a valid SearchPlan")

        if plan_model.revision != revision:
            raise ValueError("search plan revision does not match parameter")

        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            cur.execute(
                """SELECT id, payload FROM research_specs
                WHERE run_id=%s AND (id=%s OR payload->>'research_spec_id'=%s)
                ORDER BY spec_revision DESC LIMIT 1""",
                (run_id, research_spec_id, str(research_spec_id)),
            )
            spec_row = cur.fetchone()
            if spec_row is None:
                raise ValueError("search plan references an unknown research spec")

            db_spec_id, spec_payload = spec_row
            spec_model = load_model(spec_payload)

            if plan_model.research_spec_id != spec_model.research_spec_id:
                raise ValueError(
                    "search plan research_spec_id does not match research spec"
                )

            validate_references(plan_model, ValidationContext(research_spec=spec_model))

            digest = _json_sha256(plan_payload)

            cur.execute(
                """SELECT id, research_spec_id, revision, content_sha256
                FROM search_plans
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            idempotent = cur.fetchone()
            if idempotent is not None:
                stored_id, stored_spec_id, stored_revision, stored_digest = idempotent
                if (stored_spec_id, stored_revision, stored_digest) != (
                    db_spec_id,
                    revision,
                    digest,
                ):
                    raise ValueError("idempotency key was used for another search plan")
                return stored_id

            cur.execute(
                """SELECT id FROM search_plans WHERE run_id=%s AND revision=%s""",
                (run_id, revision),
            )
            existing_rev = cur.fetchone()
            if existing_rev is not None:
                raise ValueError(
                    f"search plan revision {revision} already exists for run"
                )

            cur.execute(
                """UPDATE search_plans SET status='superseded'
                WHERE run_id=%s AND status='active'""",
                (run_id,),
            )

            cur.execute(
                """INSERT INTO search_plans(
                run_id, research_spec_id, revision, schema_name, schema_version,
                status, payload, content_sha256, idempotency_key)
                VALUES(%s, %s, %s, %s, %s, 'active', %s, %s, %s)
                RETURNING id""",
                (
                    run_id,
                    db_spec_id,
                    revision,
                    plan_model.SCHEMA_VERSION,
                    1,
                    _canonical_json(plan_payload),
                    digest,
                    idempotency_key,
                ),
            )
            plan_id = cur.fetchone()[0]

            for idx, query in enumerate(plan_model.queries):
                query_payload = to_dict(query)
                freshness_dict = to_dict(query.freshness_requirement)
                cur.execute(
                    """INSERT INTO search_plan_queries(
                    id, plan_id, run_id, query_index, query_text, facet,
                    target_question_ids, target_claim_ids, intended_source_classes,
                    expected_organizations, freshness_requirement, expected_contribution,
                    domain_restrictions, negative_terms, priority, status, payload)
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
                    (
                        query.query_id,
                        plan_id,
                        run_id,
                        idx,
                        query.query,
                        query.facet,
                        json.dumps([str(qid) for qid in query.target_question_ids]),
                        json.dumps([str(cid) for cid in query.target_claim_ids]),
                        json.dumps(list(query.intended_source_classes)),
                        json.dumps(list(query.expected_organizations)),
                        _canonical_json(freshness_dict),
                        query.expected_contribution,
                        json.dumps(list(query.domain_restrictions)),
                        json.dumps(list(query.negative_terms)),
                        query.priority,
                        _canonical_json(query_payload),
                    ),
                )

            cur.execute(
                """UPDATE research_runs SET search_plan_id=%s WHERE id=%s""",
                (plan_id, run_id),
            )

            return plan_id

    def get_search_plan(self, run_id, plan_id=None, revision=None):
        with self.connection.cursor() as cur:
            if plan_id is not None:
                cur.execute(
                    """SELECT id, run_id, research_spec_id, revision, schema_name,
                    schema_version, status, payload, content_sha256, idempotency_key, created_at
                    FROM search_plans WHERE id=%s AND run_id=%s""",
                    (plan_id, run_id),
                )
            elif revision is not None:
                cur.execute(
                    """SELECT id, run_id, research_spec_id, revision, schema_name,
                    schema_version, status, payload, content_sha256, idempotency_key, created_at
                    FROM search_plans WHERE run_id=%s AND revision=%s""",
                    (run_id, revision),
                )
            else:
                cur.execute(
                    """SELECT id, run_id, research_spec_id, revision, schema_name,
                    schema_version, status, payload, content_sha256, idempotency_key, created_at
                    FROM search_plans WHERE run_id=%s ORDER BY revision DESC LIMIT 1""",
                    (run_id,),
                )
            row = cur.fetchone()
            if row is None:
                raise ValueError("search plan not found")

            (
                stored_id,
                stored_run_id,
                spec_id,
                rev,
                s_name,
                s_ver,
                status,
                payload,
                sha256,
                key,
                created_at,
            ) = row

            cur.execute(
                """SELECT id, plan_id, run_id, query_index, query_text, facet,
                target_question_ids, target_claim_ids, intended_source_classes,
                expected_organizations, freshness_requirement, expected_contribution,
                domain_restrictions, negative_terms, priority, status, payload, created_at
                FROM search_plan_queries WHERE plan_id=%s ORDER BY query_index ASC""",
                (stored_id,),
            )
            query_rows = cur.fetchall()
            queries = [
                {
                    "id": q_row[0],
                    "plan_id": q_row[1],
                    "run_id": q_row[2],
                    "query_index": q_row[3],
                    "query_text": q_row[4],
                    "facet": q_row[5],
                    "target_question_ids": q_row[6],
                    "target_claim_ids": q_row[7],
                    "intended_source_classes": q_row[8],
                    "expected_organizations": q_row[9],
                    "freshness_requirement": q_row[10],
                    "expected_contribution": q_row[11],
                    "domain_restrictions": q_row[12],
                    "negative_terms": q_row[13],
                    "priority": q_row[14],
                    "status": q_row[15],
                    "payload": q_row[16],
                    "created_at": q_row[17],
                }
                for q_row in query_rows
            ]

            return {
                "id": stored_id,
                "run_id": stored_run_id,
                "research_spec_id": spec_id,
                "revision": rev,
                "schema_name": s_name,
                "schema_version": s_ver,
                "status": status,
                "payload": payload,
                "content_sha256": sha256,
                "idempotency_key": key,
                "created_at": created_at,
                "queries": queries,
            }

    def list_search_plans(self, run_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, research_spec_id, revision, schema_name,
                schema_version, status, content_sha256, idempotency_key, created_at
                FROM search_plans WHERE run_id=%s ORDER BY revision ASC""",
                (run_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "run_id": r[1],
                    "research_spec_id": r[2],
                    "revision": r[3],
                    "schema_name": r[4],
                    "schema_version": r[5],
                    "status": r[6],
                    "content_sha256": r[7],
                    "idempotency_key": r[8],
                    "created_at": r[9],
                }
                for r in rows
            ]

    def get_plan_query(self, query_id, run_id=None):
        with self.connection.cursor() as cur:
            if run_id is not None:
                cur.execute(
                    """SELECT id, plan_id, run_id, query_index, query_text, facet,
                    target_question_ids, target_claim_ids, intended_source_classes,
                    expected_organizations, freshness_requirement, expected_contribution,
                    domain_restrictions, negative_terms, priority, status, payload, created_at
                    FROM search_plan_queries WHERE id=%s AND run_id=%s""",
                    (query_id, run_id),
                )
            else:
                cur.execute(
                    """SELECT id, plan_id, run_id, query_index, query_text, facet,
                    target_question_ids, target_claim_ids, intended_source_classes,
                    expected_organizations, freshness_requirement, expected_contribution,
                    domain_restrictions, negative_terms, priority, status, payload, created_at
                    FROM search_plan_queries WHERE id=%s""",
                    (query_id,),
                )
            row = cur.fetchone()
            if row is None:
                raise ValueError("search plan query not found")

            return {
                "id": row[0],
                "plan_id": row[1],
                "run_id": row[2],
                "query_index": row[3],
                "query_text": row[4],
                "facet": row[5],
                "target_question_ids": row[6],
                "target_claim_ids": row[7],
                "intended_source_classes": row[8],
                "expected_organizations": row[9],
                "freshness_requirement": row[10],
                "expected_contribution": row[11],
                "domain_restrictions": row[12],
                "negative_terms": row[13],
                "priority": row[14],
                "status": row[15],
                "payload": row[16],
                "created_at": row[17],
            }

    def list_plan_queries(self, plan_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, plan_id, run_id, query_index, query_text, facet,
                target_question_ids, target_claim_ids, intended_source_classes,
                expected_organizations, freshness_requirement, expected_contribution,
                domain_restrictions, negative_terms, priority, status, payload, created_at
                FROM search_plan_queries WHERE plan_id=%s ORDER BY query_index ASC""",
                (plan_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": row[0],
                    "plan_id": row[1],
                    "run_id": row[2],
                    "query_index": row[3],
                    "query_text": row[4],
                    "facet": row[5],
                    "target_question_ids": row[6],
                    "target_claim_ids": row[7],
                    "intended_source_classes": row[8],
                    "expected_organizations": row[9],
                    "freshness_requirement": row[10],
                    "expected_contribution": row[11],
                    "domain_restrictions": row[12],
                    "negative_terms": row[13],
                    "priority": row[14],
                    "status": row[15],
                    "payload": row[16],
                    "created_at": row[17],
                }
                for row in rows
            ]

    def record_search_response(
        self,
        run_id,
        query_text,
        backend,
        raw_payload,
        idempotency_key,
        blob_store,
        *,
        plan_id=None,
        plan_query_id=None,
        provider_request_id=None,
        parser_version="firecrawl-search-v1",
        http_status=None,
        error_message=None,
        requested_at=None,
        responded_at=None,
        transport_metadata=None,
        **metadata,
    ):
        """Persist an immutable raw search response and store raw payload in blob_store."""
        run_id = UUID(str(run_id))
        if plan_id is not None:
            plan_id = UUID(str(plan_id))
        if plan_query_id is not None:
            plan_query_id = UUID(str(plan_query_id))

        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("query_text must be non-empty")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be non-empty")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")

        raw_bytes = (
            raw_payload.encode("utf-8") if isinstance(raw_payload, str) else raw_payload
        )

        content_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            cur.execute(
                """SELECT id, content_sha256, query_text, backend, status, result_count,
                          raw_blob_sha256, raw_blob_bytes, mime_type, error_message,
                          payload_summary, transport_metadata, provider_request_id,
                          http_status, parser_version, plan_id, plan_query_id,
                          requested_at, responded_at, created_at
                FROM search_responses
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                (
                    ex_id,
                    ex_sha,
                    ex_query,
                    ex_backend,
                    ex_status,
                    ex_count,
                    ex_blob_sha,
                    ex_blob_bytes,
                    ex_mime,
                    ex_err,
                    ex_summary,
                    ex_transport,
                    ex_req_id,
                    ex_http_status,
                    ex_parser_ver,
                    ex_plan_id,
                    ex_plan_q_id,
                    ex_req_at,
                    ex_resp_at,
                    ex_created_at,
                ) = existing
                if ex_sha != content_sha256:
                    raise ValueError(
                        f"idempotency_key conflict: key '{idempotency_key}' already recorded with different content SHA-256"
                    )
                return {
                    "id": ex_id,
                    "run_id": run_id,
                    "plan_id": ex_plan_id,
                    "plan_query_id": ex_plan_q_id,
                    "query_text": ex_query,
                    "backend": ex_backend,
                    "provider_request_id": ex_req_id,
                    "status": ex_status,
                    "http_status": ex_http_status,
                    "parser_version": ex_parser_ver,
                    "raw_blob_sha256": ex_blob_sha,
                    "raw_blob_bytes": ex_blob_bytes,
                    "mime_type": ex_mime,
                    "content_sha256": ex_sha,
                    "result_count": ex_count,
                    "error_message": ex_err,
                    "transport_metadata": ex_transport,
                    "payload_summary": ex_summary,
                    "idempotency_key": idempotency_key,
                    "requested_at": ex_req_at,
                    "responded_at": ex_resp_at,
                    "created_at": ex_created_at,
                }

            if plan_id is not None:
                cur.execute(
                    "SELECT id FROM search_plans WHERE id=%s AND run_id=%s",
                    (plan_id, run_id),
                )
                if cur.fetchone() is None:
                    raise ValueError(
                        f"search plan {plan_id} not found for run {run_id}"
                    )

            if plan_query_id is not None:
                cur.execute(
                    "SELECT id, plan_id FROM search_plan_queries WHERE id=%s AND run_id=%s",
                    (plan_query_id, run_id),
                )
                pq_row = cur.fetchone()
                if pq_row is None:
                    raise ValueError(
                        f"search plan query {plan_query_id} not found for run {run_id}"
                    )
                if plan_id is not None and pq_row[1] != plan_id:
                    raise ValueError(
                        f"search plan query {plan_query_id} does not belong to plan {plan_id}"
                    )
                if plan_id is None:
                    plan_id = pq_row[1]

            blob_ref = blob_store.put(
                io.BytesIO(raw_bytes), mime_type="application/json"
            )

            parsed_status, parsed_result_count, parsed_summary, parsed_error = (
                parse_raw_search_response(
                    raw_bytes, http_status=http_status, parser_version=parser_version
                )
            )

            final_error = error_message or parsed_error

            now_dt = utcnow()
            req_at = requested_at or now_dt
            resp_at = responded_at or now_dt
            t_meta = dict(transport_metadata) if transport_metadata else {}
            if metadata:
                t_meta.update(metadata)

            cur.execute(
                """INSERT INTO search_responses(
                    run_id, plan_id, plan_query_id, query_text, backend,
                    provider_request_id, status, http_status, parser_version,
                    raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                    result_count, error_message, transport_metadata, payload_summary,
                    idempotency_key, requested_at, responded_at, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                ) RETURNING id, created_at""",
                (
                    run_id,
                    plan_id,
                    plan_query_id,
                    query_text,
                    backend,
                    provider_request_id,
                    parsed_status,
                    http_status,
                    parser_version,
                    blob_ref.sha256,
                    blob_ref.byte_length,
                    blob_ref.mime_type or "application/json",
                    content_sha256,
                    parsed_result_count,
                    final_error,
                    json.dumps(t_meta),
                    json.dumps(parsed_summary),
                    idempotency_key,
                    req_at,
                    resp_at,
                    now_dt,
                ),
            )
            row = cur.fetchone()
            resp_id, created_at = row[0], row[1]

            return {
                "id": resp_id,
                "run_id": run_id,
                "plan_id": plan_id,
                "plan_query_id": plan_query_id,
                "query_text": query_text,
                "backend": backend,
                "provider_request_id": provider_request_id,
                "status": parsed_status,
                "http_status": http_status,
                "parser_version": parser_version,
                "raw_blob_sha256": blob_ref.sha256,
                "raw_blob_bytes": blob_ref.byte_length,
                "mime_type": blob_ref.mime_type or "application/json",
                "content_sha256": content_sha256,
                "result_count": parsed_result_count,
                "error_message": final_error,
                "transport_metadata": t_meta,
                "payload_summary": parsed_summary,
                "idempotency_key": idempotency_key,
                "requested_at": req_at,
                "responded_at": resp_at,
                "created_at": created_at,
            }

    def get_search_response(self, response_id, run_id=None):
        response_id = UUID(str(response_id))
        with self.connection.cursor() as cur:
            query = """SELECT id, run_id, plan_id, plan_query_id, query_text, backend,
                              provider_request_id, status, http_status, parser_version,
                              raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                              result_count, error_message, transport_metadata, payload_summary,
                              idempotency_key, requested_at, responded_at, created_at
                       FROM search_responses WHERE id=%s"""
            params = [response_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))

            cur.execute(query, tuple(params))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"search response {response_id} not found")

            return {
                "id": row[0],
                "run_id": row[1],
                "plan_id": row[2],
                "plan_query_id": row[3],
                "query_text": row[4],
                "backend": row[5],
                "provider_request_id": row[6],
                "status": row[7],
                "http_status": row[8],
                "parser_version": row[9],
                "raw_blob_sha256": row[10],
                "raw_blob_bytes": row[11],
                "mime_type": row[12],
                "content_sha256": row[13],
                "result_count": row[14],
                "error_message": row[15],
                "transport_metadata": row[16],
                "payload_summary": row[17],
                "idempotency_key": row[18],
                "requested_at": row[19],
                "responded_at": row[20],
                "created_at": row[21],
            }

    def list_search_responses(
        self, run_id, *, plan_id=None, plan_query_id=None, status=None
    ):
        run_id = UUID(str(run_id))
        with self.connection.cursor() as cur:
            query = """SELECT id, run_id, plan_id, plan_query_id, query_text, backend,
                              provider_request_id, status, http_status, parser_version,
                              raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                              result_count, error_message, transport_metadata, payload_summary,
                              idempotency_key, requested_at, responded_at, created_at
                       FROM search_responses WHERE run_id=%s"""
            params = [run_id]

            if plan_id is not None:
                query += " AND plan_id=%s"
                params.append(UUID(str(plan_id)))
            if plan_query_id is not None:
                query += " AND plan_query_id=%s"
                params.append(UUID(str(plan_query_id)))
            if status is not None:
                query += " AND status=%s"
                params.append(status)

            query += " ORDER BY created_at ASC, id ASC"

            cur.execute(query, tuple(params))
            results = []
            for row in cur.fetchall():
                results.append(
                    {
                        "id": row[0],
                        "run_id": row[1],
                        "plan_id": row[2],
                        "plan_query_id": row[3],
                        "query_text": row[4],
                        "backend": row[5],
                        "provider_request_id": row[6],
                        "status": row[7],
                        "http_status": row[8],
                        "parser_version": row[9],
                        "raw_blob_sha256": row[10],
                        "raw_blob_bytes": row[11],
                        "mime_type": row[12],
                        "content_sha256": row[13],
                        "result_count": row[14],
                        "error_message": row[15],
                        "transport_metadata": row[16],
                        "payload_summary": row[17],
                        "idempotency_key": row[18],
                        "requested_at": row[19],
                        "responded_at": row[20],
                        "created_at": row[21],
                    }
                )
            return results

    def open_raw_search_response_blob(self, response_id, blob_store, run_id=None):
        resp = self.get_search_response(response_id, run_id=run_id)
        sha256_digest = resp["raw_blob_sha256"]
        return blob_store.open(sha256_digest)

    def record_response_candidates(
        self,
        run_id,
        search_response_id,
        blob_store,
        *,
        plan_id=None,
        plan_query_id=None,
    ):
        """Extract and persist search candidates and ranked occurrences from a search response."""
        run_id = UUID(str(run_id))
        search_response_id = UUID(str(search_response_id))
        if plan_id is not None:
            plan_id = UUID(str(plan_id))
        if plan_query_id is not None:
            plan_query_id = UUID(str(plan_query_id))

        resp = self.get_search_response(search_response_id, run_id=run_id)
        if plan_id is None and resp.get("plan_id") is not None:
            plan_id = resp["plan_id"]
        if plan_query_id is None and resp.get("plan_query_id") is not None:
            plan_query_id = resp["plan_query_id"]

        query_text = resp["query_text"]
        backend = resp["backend"]

        raw_blob_sha = resp["raw_blob_sha256"]
        with blob_store.open(raw_blob_sha) as handle:
            raw_bytes = handle.read()

        try:
            payload_data = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            payload_data = {}

        items = []
        if isinstance(payload_data, list):
            items = payload_data
        elif isinstance(payload_data, dict):
            for key in ("data", "results", "candidates", "items"):
                if isinstance(payload_data.get(key), list):
                    items = payload_data[key]
                    break

        occurrences_created = []

        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            for idx, item in enumerate(items, start=1):
                raw_url = None
                title = None
                snippet = None
                pub_date = None
                date_signals = {}
                backend_meta = {}

                if isinstance(item, dict):
                    raw_url = (
                        item.get("url") or item.get("link") or item.get("target_url")
                    )
                    title = item.get("title") or item.get("name")
                    snippet = (
                        item.get("snippet")
                        or item.get("description")
                        or item.get("content")
                        or item.get("markdown")
                    )
                    pub_date = (
                        item.get("published_at")
                        or item.get("publishedDate")
                        or item.get("date")
                    )
                    if pub_date:
                        date_signals["published_date"] = str(pub_date)
                    backend_meta = {
                        k: v
                        for k, v in item.items()
                        if k
                        not in (
                            "url",
                            "link",
                            "title",
                            "snippet",
                            "description",
                            "content",
                            "markdown",
                        )
                    }
                elif isinstance(item, str):
                    raw_url = item

                if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
                    continue

                try:
                    canonical_url, redacted_orig_url = canonicalize_candidate_url(
                        raw_url
                    )
                except ValueError:
                    continue

                canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                domain = urlsplit(canonical_url).hostname or "unknown"

                cur.execute(
                    """SELECT id, recurrence_count, title, snippet, date_signals, backend_metadata
                       FROM search_candidates
                       WHERE run_id=%s AND canonical_url_sha256=%s""",
                    (run_id, canonical_sha),
                )
                cand_row = cur.fetchone()
                now_dt = utcnow()

                if cand_row is not None:
                    (
                        cand_id,
                        rec_count,
                        ex_title,
                        ex_snippet,
                        ex_dates,
                        ex_backend_meta,
                    ) = cand_row
                    new_rec_count = rec_count + 1
                    updated_title = title or ex_title
                    updated_snippet = snippet or ex_snippet
                    merged_dates = {**(ex_dates or {}), **date_signals}
                    merged_backend_meta = {**(ex_backend_meta or {}), **backend_meta}

                    cur.execute(
                        """UPDATE search_candidates
                           SET recurrence_count=%s,
                               last_seen_at=%s,
                               title=%s,
                               snippet=%s,
                               date_signals=%s,
                               backend_metadata=%s
                           WHERE id=%s AND run_id=%s""",
                        (
                            new_rec_count,
                            now_dt,
                            updated_title,
                            updated_snippet,
                            json.dumps(merged_dates),
                            json.dumps(merged_backend_meta),
                            cand_id,
                            run_id,
                        ),
                    )
                else:
                    cur.execute(
                        """INSERT INTO search_candidates(
                            run_id, canonical_url, canonical_url_sha256, original_url,
                            title, snippet, domain, backend, date_signals, backend_metadata,
                            recurrence_count, first_seen_at, last_seen_at, created_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            1, %s, %s, %s
                        ) RETURNING id""",
                        (
                            run_id,
                            canonical_url,
                            canonical_sha,
                            redacted_orig_url,
                            title,
                            snippet,
                            domain,
                            backend,
                            json.dumps(date_signals),
                            json.dumps(backend_meta),
                            now_dt,
                            now_dt,
                            now_dt,
                        ),
                    )
                    cand_id = cur.fetchone()[0]

                raw_item_dict = item if isinstance(item, dict) else {"url": raw_url}

                cur.execute(
                    """INSERT INTO candidate_occurrences(
                        candidate_id, run_id, search_response_id, plan_id, plan_query_id,
                        rank, query_text, original_url, title, snippet, raw_item, discovered_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    ) ON CONFLICT (search_response_id, rank) DO UPDATE
                    SET candidate_id=EXCLUDED.candidate_id,
                        original_url=EXCLUDED.original_url,
                        title=EXCLUDED.title,
                        snippet=EXCLUDED.snippet,
                        raw_item=EXCLUDED.raw_item
                    RETURNING id""",
                    (
                        cand_id,
                        run_id,
                        search_response_id,
                        plan_id,
                        plan_query_id,
                        idx,
                        query_text,
                        redacted_orig_url,
                        title,
                        snippet,
                        json.dumps(raw_item_dict),
                        now_dt,
                    ),
                )
                occ_id = cur.fetchone()[0]
                occurrences_created.append(
                    {
                        "id": occ_id,
                        "candidate_id": cand_id,
                        "run_id": run_id,
                        "search_response_id": search_response_id,
                        "plan_id": plan_id,
                        "plan_query_id": plan_query_id,
                        "rank": idx,
                        "query_text": query_text,
                        "canonical_url": canonical_url,
                        "original_url": redacted_orig_url,
                    }
                )

        return occurrences_created

    def get_candidate(self, candidate_id, run_id=None):
        candidate_id = UUID(str(candidate_id))
        with self.connection.cursor() as cur:
            query = """SELECT id, run_id, canonical_url, canonical_url_sha256, original_url,
                              title, snippet, domain, backend, published_at, date_signals,
                              backend_metadata, recurrence_count, duplicate_group_id,
                              first_seen_at, last_seen_at, created_at
                       FROM search_candidates WHERE id=%s"""
            params = [candidate_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))

            cur.execute(query, tuple(params))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"search candidate {candidate_id} not found")

            return {
                "id": row[0],
                "run_id": row[1],
                "canonical_url": row[2],
                "canonical_url_sha256": row[3],
                "original_url": row[4],
                "title": row[5],
                "snippet": row[6],
                "domain": row[7],
                "backend": row[8],
                "published_at": row[9],
                "date_signals": row[10],
                "backend_metadata": row[11],
                "recurrence_count": row[12],
                "duplicate_group_id": row[13],
                "first_seen_at": row[14],
                "last_seen_at": row[15],
                "created_at": row[16],
            }

    def list_candidates(
        self, run_id, *, domain=None, min_recurrence=None, duplicate_group_id=None
    ):
        run_id = UUID(str(run_id))
        with self.connection.cursor() as cur:
            query = """SELECT id, run_id, canonical_url, canonical_url_sha256, original_url,
                              title, snippet, domain, backend, published_at, date_signals,
                              backend_metadata, recurrence_count, duplicate_group_id,
                              first_seen_at, last_seen_at, created_at
                       FROM search_candidates WHERE run_id=%s"""
            params = [run_id]

            if domain is not None:
                query += " AND domain=%s"
                params.append(domain)
            if min_recurrence is not None:
                query += " AND recurrence_count>=%s"
                params.append(int(min_recurrence))
            if duplicate_group_id is not None:
                query += " AND duplicate_group_id=%s"
                params.append(UUID(str(duplicate_group_id)))

            query += " ORDER BY recurrence_count DESC, created_at ASC, id ASC"

            cur.execute(query, tuple(params))
            results = []
            for row in cur.fetchall():
                results.append(
                    {
                        "id": row[0],
                        "run_id": row[1],
                        "canonical_url": row[2],
                        "canonical_url_sha256": row[3],
                        "original_url": row[4],
                        "title": row[5],
                        "snippet": row[6],
                        "domain": row[7],
                        "backend": row[8],
                        "published_at": row[9],
                        "date_signals": row[10],
                        "backend_metadata": row[11],
                        "recurrence_count": row[12],
                        "duplicate_group_id": row[13],
                        "first_seen_at": row[14],
                        "last_seen_at": row[15],
                        "created_at": row[16],
                    }
                )
            return results

    def list_candidates_paginated(
        self,
        run_id,
        *,
        plan_id=None,
        plan_query_id=None,
        query_text=None,
        domain=None,
        min_recurrence=None,
        duplicate_group_id=None,
        limit=20,
        offset=0,
    ):
        run_id = UUID(str(run_id))
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        with self.connection.cursor() as cur:
            needs_join = (
                plan_id is not None
                or plan_query_id is not None
                or query_text is not None
            )

            if needs_join:
                base_from = """FROM search_candidates c
                               JOIN candidate_occurrences o ON o.candidate_id = c.id
                               WHERE c.run_id=%s"""
                select_cols = """DISTINCT c.id, c.run_id, c.canonical_url, c.canonical_url_sha256, c.original_url,
                                  c.title, c.snippet, c.domain, c.backend, c.published_at, c.date_signals,
                                  c.backend_metadata, c.recurrence_count, c.duplicate_group_id,
                                  c.first_seen_at, c.last_seen_at, c.created_at"""
            else:
                base_from = """FROM search_candidates c WHERE c.run_id=%s"""
                select_cols = """c.id, c.run_id, c.canonical_url, c.canonical_url_sha256, c.original_url,
                                  c.title, c.snippet, c.domain, c.backend, c.published_at, c.date_signals,
                                  c.backend_metadata, c.recurrence_count, c.duplicate_group_id,
                                  c.first_seen_at, c.last_seen_at, c.created_at"""

            where_clauses = []
            params = [run_id]

            if plan_id is not None:
                where_clauses.append("o.plan_id=%s")
                params.append(UUID(str(plan_id)))
            if plan_query_id is not None:
                where_clauses.append("o.plan_query_id=%s")
                params.append(UUID(str(plan_query_id)))
            if query_text is not None:
                where_clauses.append("o.query_text=%s")
                params.append(query_text)
            if domain is not None:
                where_clauses.append("c.domain=%s")
                params.append(domain)
            if min_recurrence is not None:
                where_clauses.append("c.recurrence_count>=%s")
                params.append(int(min_recurrence))
            if duplicate_group_id is not None:
                where_clauses.append("c.duplicate_group_id=%s")
                params.append(UUID(str(duplicate_group_id)))

            where_str = ""
            if where_clauses:
                where_str = " AND " + " AND ".join(where_clauses)

            if needs_join:
                count_sql = f"SELECT COUNT(*) FROM (SELECT DISTINCT c.id {base_from}{where_str}) sub"
            else:
                count_sql = f"SELECT COUNT(*) {base_from}{where_str}"

            cur.execute(count_sql, tuple(params))
            total_count = cur.fetchone()[0]

            order_by_sql = (
                " ORDER BY c.recurrence_count DESC, c.created_at ASC, c.id ASC"
            )
            limit_sql = " LIMIT %s OFFSET %s"
            full_sql = (
                f"SELECT {select_cols} {base_from}{where_str}{order_by_sql}{limit_sql}"
            )

            query_params = list(params)
            query_params.extend([limit, offset])

            cur.execute(full_sql, tuple(query_params))
            items = []
            for row in cur.fetchall():
                items.append(
                    {
                        "id": row[0],
                        "run_id": row[1],
                        "canonical_url": row[2],
                        "canonical_url_sha256": row[3],
                        "original_url": row[4],
                        "title": row[5],
                        "snippet": row[6],
                        "domain": row[7],
                        "backend": row[8],
                        "published_at": row[9],
                        "date_signals": row[10],
                        "backend_metadata": row[11],
                        "recurrence_count": row[12],
                        "duplicate_group_id": row[13],
                        "first_seen_at": row[14],
                        "last_seen_at": row[15],
                        "created_at": row[16],
                    }
                )

            return {
                "items": items,
                "total_count": total_count,
                "limit": limit,
                "offset": offset,
                "has_next": (offset + len(items)) < total_count,
            }

    def list_candidate_occurrences(self, candidate_id, run_id=None):
        candidate_id = UUID(str(candidate_id))
        with self.connection.cursor() as cur:
            query = """SELECT id, candidate_id, run_id, search_response_id, plan_id,
                              plan_query_id, rank, query_text, original_url, title,
                              snippet, raw_item, discovered_at
                       FROM candidate_occurrences WHERE candidate_id=%s"""
            params = [candidate_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))

            query += " ORDER BY discovered_at ASC, rank ASC, id ASC"

            cur.execute(query, tuple(params))
            results = []
            for row in cur.fetchall():
                results.append(
                    {
                        "id": row[0],
                        "candidate_id": row[1],
                        "run_id": row[2],
                        "search_response_id": row[3],
                        "plan_id": row[4],
                        "plan_query_id": row[5],
                        "rank": row[6],
                        "query_text": row[7],
                        "original_url": row[8],
                        "title": row[9],
                        "snippet": row[10],
                        "raw_item": row[11],
                        "discovered_at": row[12],
                    }
                )
            return results

    def assign_duplicate_group(self, candidate_ids, group_id=None, run_id=None):
        if not candidate_ids:
            raise ValueError("candidate_ids must not be empty")
        cand_uuids = [UUID(str(cid)) for cid in candidate_ids]
        target_group_id = UUID(str(group_id)) if group_id is not None else cand_uuids[0]

        with self.connection.cursor() as cur:
            if run_id is not None:
                run_uuid = UUID(str(run_id))
                cur.execute(
                    """UPDATE search_candidates
                       SET duplicate_group_id=%s
                       WHERE id=ANY(%s) AND run_id=%s""",
                    (target_group_id, cand_uuids, run_uuid),
                )
            else:
                cur.execute(
                    """UPDATE search_candidates
                       SET duplicate_group_id=%s
                       WHERE id=ANY(%s)""",
                    (target_group_id, cand_uuids),
                )
        return target_group_id

    def record_semantic_call(
        self,
        run_id,
        stage,
        provider,
        model,
        prompt_version,
        request,
        idempotency_key,
        *,
        invocation_id=None,
        model_revision="",
        status="pending",
        expected_revision=None,
        expected_execution_mode=None,
    ):
        with self.connection.cursor() as cur:
            _state, current_revision = self._lock_workflow_run(cur, run_id)
            cur.execute(
                "SELECT execution_mode FROM research_runs WHERE id=%s", (run_id,)
            )
            current_mode = cur.fetchone()[0]
            if expected_revision is not None and current_revision != expected_revision:
                raise ValueError(
                    "stale semantic decision revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if (
                expected_execution_mode is not None
                and current_mode != expected_execution_mode
            ):
                raise ValueError(
                    "semantic authority changed before persistence: "
                    f"expected {expected_execution_mode}, current {current_mode}"
                )
            digest = _json_sha256(request)
            cur.execute(
                """INSERT INTO semantic_calls(
                run_id,invocation_id,stage,provider,model,model_revision,prompt_version,
                input_sha256,request,status,idempotency_key,started_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  CASE WHEN %s IN ('running','complete','failed','cancelled') THEN now() END)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,invocation_id,stage,provider,model,model_revision,
                  prompt_version,input_sha256,status""",
                (
                    run_id,
                    invocation_id,
                    stage,
                    provider,
                    model,
                    model_revision,
                    prompt_version,
                    digest,
                    _canonical_json(request),
                    status,
                    idempotency_key,
                    status,
                ),
            )
            row = cur.fetchone()
            expected = (
                invocation_id,
                stage,
                provider,
                model,
                model_revision,
                prompt_version,
                digest,
                status,
            )
            if row[1:] != expected:
                raise ValueError("idempotency key was used for another semantic call")
            return row[0]

    def finalize_semantic_call(
        self, run_id, call_id, status, response_metadata, error=None
    ):
        if status not in {"complete", "failed", "cancelled"}:
            raise ValueError(
                "semantic call final status must be complete, failed, or cancelled"
            )
        response_json = _canonical_json(response_metadata or {})
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT status,response_metadata,error FROM semantic_calls
                WHERE id=%s AND run_id=%s FOR UPDATE""",
                (call_id, run_id),
            )
            existing = cur.fetchone()
            if existing is None:
                raise ValueError("semantic call does not belong to the research run")
            expected = (status, json.loads(response_json), error)
            if existing[0] in {"complete", "failed", "cancelled"}:
                if existing != expected:
                    raise ValueError("semantic call was already finalized differently")
                return call_id
            cur.execute(
                """UPDATE semantic_calls SET status=%s,response_metadata=%s,error=%s,
                completed_at=now(),started_at=COALESCE(started_at,created_at)
                WHERE id=%s AND run_id=%s RETURNING id""",
                (status, response_json, error, call_id, run_id),
            )
            return cur.fetchone()[0]

    def annotate_semantic_call(self, run_id, call_id, metadata):
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """UPDATE semantic_calls
                SET response_metadata=response_metadata || %s::jsonb
                WHERE id=%s AND run_id=%s RETURNING id""",
                (_canonical_json(metadata or {}), call_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("semantic call does not belong to the research run")
            return row[0]

    def get_semantic_call(self, run_id, call_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id,run_id,invocation_id,stage,provider,model,model_revision,
                prompt_version,input_sha256,request,response_metadata,status,error,
                started_at,completed_at,created_at FROM semantic_calls
                WHERE id=%s AND run_id=%s""",
                (call_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("semantic call does not belong to the research run")
            keys = (
                "id",
                "run_id",
                "invocation_id",
                "stage",
                "provider",
                "model",
                "model_revision",
                "prompt_version",
                "input_sha256",
                "request",
                "response_metadata",
                "status",
                "error",
                "started_at",
                "completed_at",
                "created_at",
            )
            result = dict(zip(keys, row))
            cur.execute(
                """SELECT id,artifact_type,schema_name,schema_version,payload,
                content_sha256,validation_status,validation_errors,created_at
                FROM semantic_artifacts WHERE semantic_call_id=%s AND run_id=%s
                ORDER BY created_at,id""",
                (call_id, run_id),
            )
            artifact_keys = (
                "id",
                "artifact_type",
                "schema_name",
                "schema_version",
                "payload",
                "content_sha256",
                "validation_status",
                "validation_errors",
                "created_at",
            )
            result["artifacts"] = [
                dict(zip(artifact_keys, item)) for item in cur.fetchall()
            ]
            return result

    def record_semantic_artifact(
        self,
        run_id,
        semantic_call_id,
        artifact_type,
        schema_name,
        schema_version,
        payload,
        idempotency_key,
        *,
        validation_status="valid",
        validation_errors=None,
    ):
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            digest = _json_sha256(payload)
            cur.execute(
                """INSERT INTO semantic_artifacts(
                run_id,semantic_call_id,artifact_type,schema_name,schema_version,payload,
                content_sha256,validation_status,validation_errors,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(semantic_call_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,artifact_type,schema_name,schema_version,content_sha256,
                  validation_status,validation_errors""",
                (
                    run_id,
                    semantic_call_id,
                    artifact_type,
                    schema_name,
                    schema_version,
                    _canonical_json(payload),
                    digest,
                    validation_status,
                    _canonical_json(validation_errors or []),
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            expected = (
                artifact_type,
                schema_name,
                schema_version,
                digest,
                validation_status,
                validation_errors or [],
            )
            if row[1:] != expected:
                raise ValueError(
                    "idempotency key was used for another semantic artifact"
                )
            return row[0]

    def record_compatibility_export(
        self,
        run_id,
        export_type,
        export_schema_version,
        source_state_sha256,
        status,
        idempotency_key,
        *,
        invocation_id=None,
        database_revision=None,
        event_cursor=None,
        blob_uri=None,
        filesystem_path=None,
        error=None,
        metadata=None,
    ):
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """INSERT INTO compatibility_exports(
                run_id,invocation_id,export_type,export_schema_version,database_revision,
                event_cursor,source_state_sha256,blob_uri,filesystem_path,status,error,
                metadata,idempotency_key,completed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  CASE WHEN %s IN ('complete','failed') THEN now() ELSE NULL END)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,export_type,source_state_sha256""",
                (
                    run_id,
                    invocation_id,
                    export_type,
                    export_schema_version,
                    database_revision,
                    event_cursor,
                    source_state_sha256,
                    blob_uri,
                    filesystem_path,
                    status,
                    error,
                    _canonical_json(metadata or {}),
                    idempotency_key,
                    status,
                ),
            )
            export_id, stored_type, stored_hash = cur.fetchone()
            if (stored_type, stored_hash) != (export_type, source_state_sha256):
                raise ValueError("idempotency key was used for another export")
            return export_id

    def record_legacy_adapter_comparison(
        self,
        entry_point,
        adapter_mode,
        legacy_decision,
        service_proposal,
        legacy_sha256,
        proposal_sha256,
        divergent,
        divergence_reasons,
        idempotency_key,
        *,
        run_id=None,
        external_run_id=None,
        external_invocation_id=None,
        workflow_revision=None,
    ):
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO legacy_adapter_comparisons(
                run_id,external_run_id,external_invocation_id,entry_point,
                adapter_mode,legacy_decision,service_proposal,legacy_sha256,
                proposal_sha256,divergent,divergence_reasons,workflow_revision,
                idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(idempotency_key) DO NOTHING
                RETURNING id,run_id,external_run_id,external_invocation_id,
                  entry_point,adapter_mode,legacy_sha256,proposal_sha256,divergent,
                  divergence_reasons,workflow_revision""",
                (
                    run_id,
                    external_run_id,
                    external_invocation_id,
                    entry_point,
                    adapter_mode,
                    _canonical_json(legacy_decision),
                    _canonical_json(service_proposal),
                    legacy_sha256,
                    proposal_sha256,
                    divergent,
                    _canonical_json(divergence_reasons),
                    workflow_revision,
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """SELECT id,run_id,external_run_id,external_invocation_id,
                    entry_point,adapter_mode,legacy_sha256,proposal_sha256,divergent,
                    divergence_reasons,workflow_revision
                    FROM legacy_adapter_comparisons WHERE idempotency_key=%s""",
                    (idempotency_key,),
                )
                row = cur.fetchone()
            expected = (
                run_id,
                external_run_id,
                external_invocation_id,
                entry_point,
                adapter_mode,
                legacy_sha256,
                proposal_sha256,
                divergent,
                divergence_reasons,
                workflow_revision,
            )
            if row[1:] != expected:
                raise ValueError(
                    "idempotency key was used for another legacy adapter comparison"
                )
            return row[0]

    def list_legacy_adapter_comparisons(
        self,
        *,
        external_run_id=None,
        external_invocation_id=None,
        entry_point=None,
        divergent_only=False,
        limit=100,
    ):
        if not 1 <= limit <= 1000:
            raise ValueError("comparison query limit must be 1..1000")
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id,run_id,external_run_id,external_invocation_id,
                entry_point,adapter_mode,legacy_decision,service_proposal,
                legacy_sha256,proposal_sha256,divergent,divergence_reasons,
                workflow_revision,idempotency_key,created_at
                FROM legacy_adapter_comparisons
                WHERE (%s::text IS NULL OR external_run_id=%s)
                  AND (%s::text IS NULL OR external_invocation_id=%s)
                  AND (%s::text IS NULL OR entry_point=%s)
                  AND (NOT %s OR divergent)
                ORDER BY created_at,id LIMIT %s""",
                (
                    external_run_id,
                    external_run_id,
                    external_invocation_id,
                    external_invocation_id,
                    entry_point,
                    entry_point,
                    divergent_only,
                    limit,
                ),
            )
            keys = (
                "id",
                "run_id",
                "external_run_id",
                "external_invocation_id",
                "entry_point",
                "adapter_mode",
                "legacy_decision",
                "service_proposal",
                "legacy_sha256",
                "proposal_sha256",
                "divergent",
                "divergence_reasons",
                "workflow_revision",
                "idempotency_key",
                "created_at",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def link_run_asset(
        self, external_run_id, snapshot_id, role="acquired", metadata=None
    ):
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
                    raise ValueError("ingestion batches require a running research run")
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
        self,
        batch_id,
        ordinal,
        requested_url,
        status,
        result=None,
        error=None,
        metadata=None,
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
            keys = (
                "batch_id",
                "invocation_id",
                "operation",
                "status",
                "started_at",
                "completed_at",
                "error",
                "metadata",
                "research_run_id",
            )
            result = dict(zip(keys, row))
            cur.execute(
                """SELECT ordinal,requested_url,status,source_id,snapshot_id,document_id,chunk_ids,error,metadata
                FROM ingestion_batch_assets WHERE batch_id=%s ORDER BY ordinal""",
                (row[0],),
            )
            asset_keys = (
                "ordinal",
                "requested_url",
                "status",
                "source_id",
                "snapshot_id",
                "document_id",
                "chunk_ids",
                "error",
                "metadata",
            )
            result["assets"] = [
                dict(zip(asset_keys, asset)) for asset in cur.fetchall()
            ]
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
                f"""INSERT INTO retrieval_events(run_id,{",".join(fields)})
                SELECT id,{",".join(["%s"] * len(fields))}
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
                (
                    max_attempts,
                    fingerprint,
                    fingerprint,
                    limit,
                    worker_id,
                    lease_seconds,
                ),
            )
            keys = (
                "id",
                "manifest_id",
                "index_definition_id",
                "entity_id",
                "operation",
                "attempt_count",
                "lease_token",
                "fingerprint",
                "physical_collection",
                "model_name",
                "model_revision",
                "dimension",
                "distance_metric",
                "normalization",
                "instruction_template_hash",
            )
            return [{**dict(zip(keys, r)), "chunk_id": r[3]} for r in cur.fetchall()]

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
            raise TypeError(
                "finish_job requires the UUID lease token returned by claim_jobs"
            )
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

    # ------------------------------------------------------------------
    # Coverage event repository
    # ------------------------------------------------------------------

    def create_items(
        self,
        run_id,
        items,
        idempotency_key,
        source_event_id=None,
        source_invocation_id=None,
        execution_mode="deterministic_debug",
    ):
        """Seed coverage items from a ResearchSpec.

        Returns the list of coverage item IDs created.
        """
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            item_ids = []
            for item in items:
                cur.execute(
                    """INSERT INTO coverage_events(
                        run_id, coverage_revision, prior_coverage_revision,
                        event_type, item_id, item_type, subject_id,
                        new_status, previous_status,
                        source_event_id, source_invocation_id,
                        payload, idempotency_key
                    ) VALUES(%s, 1, 0, 'item_created', gen_random_uuid(), %s, %s,
                        'unassessed', NULL,
                        %s, %s,
                        %s, %s)
                    ON CONFLICT(run_id, idempotency_key) DO NOTHING
                    RETURNING item_id""",
                    (
                        run_id,
                        item["item_type"],
                        item["subject_id"],
                        source_event_id,
                        source_invocation_id,
                        json.dumps(
                            {
                                "execution_mode": execution_mode,
                                "text": item.get("text", ""),
                            }
                        ),
                        idempotency_key,
                    ),
                )
                row = cur.fetchone()
                if row:
                    item_ids.append(row[0])
            if not item_ids:
                # Already existed — return existing IDs
                cur.execute(
                    """SELECT item_id FROM coverage_events
                    WHERE run_id=%s AND event_type='item_created'
                      AND item_type=ANY(%s)
                    ORDER BY created_at, id""",
                    (
                        run_id,
                        [item["item_type"] for item in items],
                    ),
                )
                item_ids = [r[0] for r in cur.fetchall()]
            # Update run's current_coverage_revision to at least 1
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision = 1 WHERE id = %s AND current_coverage_revision < 1",
                (run_id,),
            )
            return item_ids

    def apply_event(
        self,
        run_id,
        event_type,
        item_id=None,
        item_type=None,
        subject_id=None,
        new_status=None,
        previous_status=None,
        new_freshness_status=None,
        previous_freshness_status=None,
        source_event_id=None,
        source_invocation_id=None,
        payload=None,
        idempotency_key=None,
    ):
        """Apply one coverage event.

        Returns the event row as a dict.  Raises ValueError for stale
        revisions or unknown items.
        """
        payload = payload or {}
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            # Check idempotency
            cur.execute(
                """SELECT id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                keys = (
                    "id",
                    "coverage_revision",
                    "prior_coverage_revision",
                    "event_type",
                    "item_id",
                    "item_type",
                    "subject_id",
                    "new_status",
                    "previous_status",
                    "new_freshness_status",
                    "previous_freshness_status",
                    "source_event_id",
                    "source_invocation_id",
                    "payload",
                    "idempotency_key",
                    "created_at",
                )
                return dict(zip(keys, existing))

            # Get current revision
            cur.execute(
                "SELECT current_coverage_revision FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(run_id)
            current_revision = row[0]

            new_revision = current_revision + 1

            # Validate revision ordering
            if new_revision <= current_revision:
                raise ValueError(
                    f"stale coverage revision: proposed {new_revision} "
                    f"does not exceed current {current_revision}"
                )

            # Validate item reference if provided
            if item_id is not None:
                cur.execute(
                    """SELECT 1 FROM coverage_events
                    WHERE run_id=%s AND item_id=%s AND event_type='item_created'
                    LIMIT 1""",
                    (run_id, item_id),
                )
                if not cur.fetchone():
                    raise ValueError(
                        f"unknown coverage item {item_id} for run {run_id}"
                    )

            # Insert the event
            cur.execute(
                """INSERT INTO coverage_events(
                    run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at""",
                (
                    run_id,
                    new_revision,
                    current_revision,
                    event_type,
                    item_id,
                    item_type,
                    subject_id,
                    new_status,
                    previous_status,
                    new_freshness_status,
                    previous_freshness_status,
                    source_event_id,
                    source_invocation_id,
                    json.dumps(payload),
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            keys = (
                "id",
                "run_id",
                "coverage_revision",
                "prior_coverage_revision",
                "event_type",
                "item_id",
                "item_type",
                "subject_id",
                "new_status",
                "previous_status",
                "new_freshness_status",
                "previous_freshness_status",
                "source_event_id",
                "source_invocation_id",
                "payload",
                "idempotency_key",
                "created_at",
            )
            result = dict(zip(keys, row))

            # Update run's current_coverage_revision
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision=%s WHERE id=%s",
                (new_revision, run_id),
            )

            return result

    def rebuild_projection(
        self,
        run_id,
        idempotency_key,
        source_event_id=None,
    ):
        """Rebuild the current coverage projection from events.

        Returns a ledger dict that can be materialized as a snapshot.
        """
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            # Get all events in deterministic order
            cur.execute(
                """SELECT event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    payload
                FROM coverage_events
                WHERE run_id=%s
                ORDER BY coverage_revision, id""",
                (run_id,),
            )
            events = cur.fetchall()

            # Build item state from events
            items = {}
            max_revision = 0
            for evt in events:
                (
                    event_type,
                    item_id,
                    item_type,
                    subject_id,
                    new_status,
                    previous_status,
                    new_freshness_status,
                    previous_freshness_status,
                    payload,
                ) = evt

                if event_type == "item_created":
                    items[str(item_id)] = {
                        "coverage_item_id": str(item_id),
                        "item_type": item_type or "question",
                        "subject_id": subject_id or "",
                        "status": "unassessed",
                        "freshness_status": "not_applicable",
                        "candidate_ids": [],
                        "snapshot_ids": [],
                        "passage_ids": [],
                        "independent_source_count": 0,
                        "required_independent_source_count": 0,
                        "authority_classes_present": [],
                        "remaining_gap": (payload or {}).get("text", ""),
                        "confidence": 0.0,
                    }
                elif event_type == "item_status_changed" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = new_status or items[key]["status"]
                        items[key]["confidence"] = (payload or {}).get(
                            "confidence", items[key].get("confidence", 0.0)
                        )
                        items[key]["remaining_gap"] = (payload or {}).get(
                            "remaining_gap", items[key].get("remaining_gap", "")
                        )
                        if "candidate_ids" in (payload or {}):
                            items[key]["candidate_ids"] = [
                                str(cid) for cid in payload["candidate_ids"]
                            ]
                        if "snapshot_ids" in (payload or {}):
                            items[key]["snapshot_ids"] = [
                                str(sid) for sid in payload["snapshot_ids"]
                            ]
                        if "passage_ids" in (payload or {}):
                            items[key]["passage_ids"] = [
                                str(pid) for pid in payload["passage_ids"]
                            ]
                        if "independent_source_count" in (payload or {}):
                            items[key]["independent_source_count"] = payload[
                                "independent_source_count"
                            ]
                        if "authority_classes_present" in (payload or {}):
                            items[key]["authority_classes_present"] = payload[
                                "authority_classes_present"
                            ]
                    else:
                        # Item was created by a later event — create it
                        items[key] = {
                            "coverage_item_id": str(item_id),
                            "item_type": item_type or "question",
                            "subject_id": subject_id or "",
                            "status": new_status or "unassessed",
                            "freshness_status": "not_applicable",
                            "candidate_ids": [],
                            "snapshot_ids": [],
                            "passage_ids": [],
                            "independent_source_count": 0,
                            "required_independent_source_count": 0,
                            "authority_classes_present": [],
                            "remaining_gap": "",
                            "confidence": 0.0,
                        }
                elif event_type == "item_gap_identified" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "blocked"
                elif event_type == "item_gap_resolved" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "satisfied"

                # ------------------------------------------------------------------
                # Workflow observation events (FR-012, FR-013)
                # These record deterministic observations, NOT semantic support.
                # ------------------------------------------------------------------

                elif event_type == "candidate_identified" and item_id:
                    key = str(item_id)
                    if key in items:
                        candidate_id = (payload or {}).get("candidate_id")
                        if candidate_id:
                            cid = str(candidate_id)
                            if cid not in items[key]["candidate_ids"]:
                                items[key]["candidate_ids"].append(cid)

                elif event_type == "extraction_attempted" and item_id:
                    key = str(item_id)
                    if key in items:
                        # Record extraction attempt; does not change status.
                        # Payload may contain source_url, extraction_status.
                        # Source URL is tracked for independent-source counting
                        # because an extraction attempt indicates a source was
                        # engaged, even if the attempt did not result in
                        # successful acquisition.  The status remains unassessed
                        # — engagement alone does not imply support.
                        source_url = (payload or {}).get("source_url")
                        if source_url:
                            items[key].setdefault("_source_urls", []).append(
                                str(source_url)
                            )

                elif event_type == "asset_acquired" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "acquired"
                        # Track unique source URLs for independent-source count.
                        source_url = (payload or {}).get("source_url")
                        if source_url:
                            items[key].setdefault("_source_urls", []).append(
                                str(source_url)
                            )
                        # Allow payload to carry an explicit count (e.g. from
                        # a higher-level service that has already deduplicated).
                        if "independent_source_count" in (payload or {}):
                            items[key]["independent_source_count"] = payload[
                                "independent_source_count"
                            ]

                elif event_type == "evidence_retrieved" and item_id:
                    key = str(item_id)
                    if key in items:
                        # Evidence retrieval is an observation, NOT a semantic
                        # support judgment.  Status is NOT changed to supported.
                        passage_ids = (payload or {}).get("passage_ids", [])
                        for pid in passage_ids:
                            pid_str = str(pid)
                            if pid_str not in items[key]["passage_ids"]:
                                items[key]["passage_ids"].append(pid_str)

                elif event_type == "source_class_observed" and item_id:
                    key = str(item_id)
                    if key in items:
                        authority_class = (payload or {}).get("authority_class")
                        if authority_class:
                            if (
                                authority_class
                                not in items[key]["authority_classes_present"]
                            ):
                                items[key]["authority_classes_present"].append(
                                    authority_class
                                )

                elif event_type == "freshness_observed" and item_id:
                    key = str(item_id)
                    if key in items:
                        fs = (payload or {}).get("freshness_status")
                        if fs:
                            items[key]["freshness_status"] = fs

                # ------------------------------------------------------------------
                # End of workflow observation events
                # ------------------------------------------------------------------

                if event_type != "snapshot_created":
                    max_revision = max(
                        max_revision,
                        int(payload.get("coverage_revision", 0))
                        if isinstance(payload, dict)
                        and "coverage_revision" in (payload or {})
                        else 0,
                    )

            # ------------------------------------------------------------------
            # Post-process: deduplicate source URLs → independent_source_count.
            # Independent-source counts must NOT be derived from raw event counts
            # (URLs, candidate occurrences, extraction attempts, or chunk counts).
            # Instead we use unique source URLs as a stable grouping proxy.
            # ------------------------------------------------------------------
            for item in items.values():
                source_urls = item.pop("_source_urls", [])
                if source_urls:
                    item["independent_source_count"] = len(set(source_urls))

            # ------------------------------------------------------------------
            # Calculate overall status
            if not items:
                overall_status = "unassessed"
            else:
                satisfied = sum(
                    1
                    for item in items.values()
                    if item["status"] in ("satisfied", "waived")
                )
                blocked = sum(
                    1 for item in items.values() if item["status"] == "blocked"
                )
                total = len(items)
                if satisfied == total:
                    overall_status = "sufficient"
                elif blocked > 0:
                    overall_status = "blocked"
                elif satisfied > 0:
                    overall_status = "partial"
                else:
                    overall_status = "insufficient"

            # Get the current revision from the max event
            cur.execute(
                "SELECT COALESCE(MAX(coverage_revision), 0) FROM coverage_events WHERE run_id=%s",
                (run_id,),
            )
            current_rev = cur.fetchone()[0]
            if current_rev == 0:
                current_rev = 1

            ledger = {
                "schema_version": "coverage-ledger-v1",
                "run_id": str(run_id),
                "revision": current_rev,
                "items": list(items.values()),
                "overall_status": overall_status,
            }

            # Create a projection snapshot event
            cur.execute(
                """INSERT INTO coverage_events(
                    run_id, coverage_revision, prior_coverage_revision,
                    event_type, source_event_id,
                    payload, idempotency_key
                ) VALUES(%s, %s, %s, 'projection_rebuilt', %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id""",
                (
                    run_id,
                    current_rev + 1,
                    current_rev,
                    source_event_id,
                    json.dumps(
                        {
                            "item_count": len(items),
                            "overall_status": overall_status,
                            "source_event_id": str(source_event_id)
                            if source_event_id
                            else None,
                            "coverage_revision": current_rev,
                        }
                    ),
                    idempotency_key,
                ),
            )

            return ledger

    def create_snapshot(
        self,
        run_id,
        coverage_revision,
        ledger,
        content_sha256,
        idempotency_key,
        triggering_event_id=None,
    ):
        """Materialize an immutable ledger snapshot."""
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)

            # Check idempotency
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots
                WHERE run_id=%s AND coverage_revision=%s""",
                (run_id, coverage_revision),
            )
            existing = cur.fetchone()
            if existing:
                keys = (
                    "id",
                    "run_id",
                    "coverage_revision",
                    "ledger",
                    "content_sha256",
                    "triggering_event_id",
                    "created_at",
                )
                return dict(zip(keys, existing))

            cur.execute(
                """INSERT INTO coverage_snapshots(
                    run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id
                ) VALUES(%s, %s, %s, %s, %s)
                RETURNING id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at""",
                (
                    run_id,
                    coverage_revision,
                    json.dumps(ledger) if isinstance(ledger, dict) else ledger,
                    content_sha256,
                    triggering_event_id,
                ),
            )
            row = cur.fetchone()
            keys = (
                "id",
                "run_id",
                "coverage_revision",
                "ledger",
                "content_sha256",
                "triggering_event_id",
                "created_at",
            )
            result = dict(zip(keys, row))

            # Update run's current_coverage_revision
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision=%s WHERE id=%s",
                (coverage_revision, run_id),
            )

            return result

    def get_snapshot(self, run_id, coverage_revision):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots
                WHERE run_id=%s AND coverage_revision=%s""",
                (run_id, coverage_revision),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "ledger",
            "content_sha256",
            "triggering_event_id",
            "created_at",
        )
        return dict(zip(keys, row))

    def get_latest_snapshot(self, run_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots
                WHERE run_id=%s
                ORDER BY coverage_revision DESC LIMIT 1""",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "ledger",
            "content_sha256",
            "triggering_event_id",
            "created_at",
        )
        return dict(zip(keys, row))

    def list_events(
        self,
        run_id,
        item_id=None,
        event_type=None,
        limit=100,
        offset=0,
    ):
        conditions = ["run_id = %s"]
        params = [run_id]
        if item_id is not None:
            conditions.append("item_id = %s")
            params.append(item_id)
        if event_type is not None:
            conditions.append("event_type = %s")
            params.append(event_type)

        where = " AND ".join(conditions)
        with self.connection.cursor() as cur:
            cur.execute(
                f"""SELECT id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events
                WHERE {where}
                ORDER BY coverage_revision, id
                LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            keys = (
                "id",
                "run_id",
                "coverage_revision",
                "prior_coverage_revision",
                "event_type",
                "item_id",
                "item_type",
                "subject_id",
                "new_status",
                "previous_status",
                "new_freshness_status",
                "previous_freshness_status",
                "source_event_id",
                "source_invocation_id",
                "payload",
                "idempotency_key",
                "created_at",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def get_event(self, run_id, event_id):
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events
                WHERE run_id=%s AND id=%s""",
                (run_id, event_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "prior_coverage_revision",
            "event_type",
            "item_id",
            "item_type",
            "subject_id",
            "new_status",
            "previous_status",
            "new_freshness_status",
            "previous_freshness_status",
            "source_event_id",
            "source_invocation_id",
            "payload",
            "idempotency_key",
            "created_at",
        )
        return dict(zip(keys, row))

    def get_current_revision(self, run_id):
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(current_coverage_revision, 0) FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return 0
        return row[0]

    def count_events(self, run_id):
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM coverage_events WHERE run_id=%s",
                (run_id,),
            )
            return cur.fetchone()[0]

    def count_coverage_items(self, run_id):
        """Return the number of coverage items (item_created events) for a run."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM coverage_events
                   WHERE run_id=%s AND event_type='item_created'""",
                (run_id,),
            )
            return cur.fetchone()[0]

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

    # ------------------------------------------------------------------
    # Strategy revision repository
    # ------------------------------------------------------------------

    def record_proposal(
        self,
        run_id,
        proposal_id,
        run_revision,
        coverage_revision,
        decision_type,
        target_coverage_item_ids,
        proposed_queries,
        proposed_candidate_ids,
        proposed_retrieval_queries,
        expected_contribution,
        estimated_cost,
        rationale,
        confidence,
        idempotency_key,
        **metadata,
    ):
        """Persist a strategy-revision proposal row.

        Idempotent on (run_id, idempotency_key).  Returns the row id.

        revision_order is computed as ``COALESCE(MAX(revision_order), 0) + 1``
        for the given run_id.  This subquery is not atomic with respect to the
        INSERT, so correctness relies on ``_lock_workflow_run`` holding an
        advisory lock on ``run_id`` for the entire cursor scope.  Without that
        lock, concurrent inserts could read the same ``MAX(revision_order)``
        and produce duplicate ``revision_order`` values.
        """
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """INSERT INTO strategy_revisions(
                    run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_type,
                    target_coverage_item_ids,
                    proposed_queries,
                    proposed_candidate_ids,
                    proposed_retrieval_queries,
                    expected_contribution,
                    estimated_cost,
                    rationale,
                    confidence,
                    idempotency_key,
                    actor_type,
                    actor_identifier
                ) VALUES(%s, %s, %s,
                    (SELECT COALESCE(MAX(revision_order), 0) + 1
                     FROM strategy_revisions WHERE run_id=%s),
                    'proposal',
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_type,
                    target_coverage_item_ids,
                    proposed_queries,
                    proposed_candidate_ids,
                    proposed_retrieval_queries,
                    expected_contribution,
                    estimated_cost,
                    rationale,
                    confidence,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at""",
                (
                    run_id,
                    run_revision,
                    coverage_revision,
                    run_id,
                    proposal_id,
                    decision_type,
                    json.dumps(target_coverage_item_ids),
                    json.dumps(proposed_queries),
                    json.dumps(proposed_candidate_ids),
                    json.dumps(proposed_retrieval_queries),
                    expected_contribution,
                    json.dumps(estimated_cost),
                    rationale,
                    confidence,
                    idempotency_key,
                    metadata.get("actor_type", "system"),
                    metadata.get("actor_identifier"),
                ),
            )
            row = cur.fetchone()
            if row is None:
                # Already existed — return existing
                return self._get_proposal_row(cur, run_id, proposal_id)
            return self._row_to_proposal_mapping(row)

    def get_proposal(self, run_id, proposal_id):
        """Retrieve a strategy-revision proposal by run and proposal_id."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_type,
                    target_coverage_item_ids,
                    proposed_queries,
                    proposed_candidate_ids,
                    proposed_retrieval_queries,
                    expected_contribution,
                    estimated_cost,
                    rationale,
                    confidence,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at
                FROM strategy_revisions
                WHERE run_id=%s AND proposal_id=%s AND row_type='proposal'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id, proposal_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_proposal_mapping(row)

    def list_proposals(
        self, run_id, *, run_revision=None, coverage_revision=None, limit=100, offset=0
    ):
        """List proposals for a run, optionally filtered by revision."""
        with self.connection.cursor() as cur:
            where = "WHERE run_id=%s AND row_type='proposal'"
            params: list = [run_id]
            if run_revision is not None:
                where += " AND run_revision=%s"
                params.append(run_revision)
            if coverage_revision is not None:
                where += " AND coverage_revision=%s"
                params.append(coverage_revision)
            where += " ORDER BY revision_order DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cur.execute(
                f"""SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_type,
                    target_coverage_item_ids,
                    proposed_queries,
                    proposed_candidate_ids,
                    proposed_retrieval_queries,
                    expected_contribution,
                    estimated_cost,
                    rationale,
                    confidence,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at
                FROM strategy_revisions {where}""",
                params,
            )
            return [self._row_to_proposal_mapping(r) for r in cur.fetchall()]

    def record_decision(
        self,
        run_id,
        decision_id,
        proposal_id,
        run_revision,
        coverage_revision,
        outcome,
        rejection_reasons,
        policy_version,
        scope_expansion_type,
        scope_expansion_rationale,
        scope_expansion_approved,
        authorized_by,
        idempotency_key,
        **metadata,
    ):
        """Persist a strategy-revision decision row.

        Idempotent on (run_id, idempotency_key).  Returns the row id.

        revision_order is computed as ``COALESCE(MAX(revision_order), 0) + 1``
        for the given run_id.  This subquery is not atomic with respect to the
        INSERT, so correctness relies on ``_lock_workflow_run`` holding an
        advisory lock on ``run_id`` for the entire cursor scope.
        """
        with self.connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """INSERT INTO strategy_revisions(
                    run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_id,
                    outcome,
                    rejection_reasons,
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    actor_type,
                    actor_identifier
                ) VALUES(%s, %s, %s,
                    (SELECT COALESCE(MAX(revision_order), 0) + 1
                     FROM strategy_revisions WHERE run_id=%s),
                    'decision',
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_id,
                    outcome,
                    rejection_reasons,
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at""",
                (
                    run_id,
                    run_revision,
                    coverage_revision,
                    run_id,
                    proposal_id,
                    decision_id,
                    outcome,
                    rejection_reasons or [],
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    metadata.get("actor_type", "system"),
                    metadata.get("actor_identifier"),
                ),
            )
            row = cur.fetchone()
            if row is None:
                # Already existed — return existing
                return self._get_decision_row(cur, run_id, decision_id)
            return self._row_to_decision_mapping(row)

    def get_decision(self, run_id, decision_id):
        """Retrieve a strategy-revision decision by run and decision_id."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_id,
                    outcome,
                    rejection_reasons,
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at
                FROM strategy_revisions
                WHERE run_id=%s AND decision_id=%s AND row_type='decision'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id, decision_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_decision_mapping(row)

    def list_decisions(
        self, run_id, *, proposal_id=None, outcome=None, limit=100, offset=0
    ):
        """List decisions for a run or proposal, optionally filtered."""
        with self.connection.cursor() as cur:
            where = "WHERE run_id=%s AND row_type='decision'"
            params: list = [run_id]
            if proposal_id is not None:
                where += " AND proposal_id=%s"
                params.append(proposal_id)
            if outcome is not None:
                where += " AND outcome=%s"
                params.append(outcome)
            where += " ORDER BY revision_order DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cur.execute(
                f"""SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_id,
                    outcome,
                    rejection_reasons,
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at
                FROM strategy_revisions {where}""",
                params,
            )
            return [self._row_to_decision_mapping(r) for r in cur.fetchall()]

    def proposal_exists(self, run_id, proposal_id):
        """Check if a proposal exists for the given run and proposal_id."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM strategy_revisions
                WHERE run_id=%s AND proposal_id=%s AND row_type='proposal'""",
                (run_id, proposal_id),
            )
            return cur.fetchone()[0] > 0

    def get_proposal_by_idempotency(self, run_id, idempotency_key):
        """Fetch a proposal by run_id and idempotency_key."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type,
                    proposal_id, decision_type,
                    target_coverage_item_ids,
                    proposed_queries,
                    proposed_candidate_ids,
                    proposed_retrieval_queries,
                    expected_contribution,
                    estimated_cost,
                    rationale,
                    confidence,
                    idempotency_key,
                    actor_type,
                    actor_identifier,
                    created_at
                FROM strategy_revisions
                WHERE run_id=%s AND idempotency_key=%s AND row_type='proposal'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id, idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_proposal_mapping(row)

    def decision_exists(self, run_id, decision_id):
        """Check if a decision exists for the given run and decision_id."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM strategy_revisions
                WHERE run_id=%s AND decision_id=%s AND row_type='decision'""",
                (run_id, decision_id),
            )
            return cur.fetchone()[0] > 0

    def list_proposal_ids_for_run(self, run_id):
        """List all proposal IDs for a run."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT proposal_id FROM strategy_revisions
                WHERE run_id=%s AND row_type='proposal'
                ORDER BY proposal_id""",
                (run_id,),
            )
            return [str(r[0]) for r in cur.fetchall()]

    def list_decision_ids_for_proposal(self, run_id, proposal_id):
        """List all decision IDs for a proposal."""
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT decision_id FROM strategy_revisions
                WHERE run_id=%s AND proposal_id=%s AND row_type='decision'
                ORDER BY revision_order""",
                (run_id, proposal_id),
            )
            return [str(r[0]) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Strategy revision helpers
    # ------------------------------------------------------------------

    def _row_to_proposal_mapping(self, row):
        """Convert a strategy_revisions proposal row to a mapping."""
        return {
            "id": str(row[0]),
            "run_id": str(row[1]),
            "run_revision": row[2],
            "coverage_revision": row[3],
            "revision_order": row[4],
            "row_type": row[5],
            "proposal_id": str(row[6]),
            "decision_type": row[7],
            "target_coverage_item_ids": row[8] or [],
            "proposed_queries": row[9] or [],
            "proposed_candidate_ids": row[10] or [],
            "proposed_retrieval_queries": row[11] or [],
            "expected_contribution": row[12] or "",
            "estimated_cost": row[13] or {},
            "rationale": row[14] or "",
            "confidence": row[15] or 0.0,
            "idempotency_key": row[16],
            "actor_type": row[17] or "system",
            "actor_identifier": row[18],
            "created_at": row[19],
        }

    def _row_to_decision_mapping(self, row):
        """Convert a strategy_revisions decision row to a mapping."""
        return {
            "id": str(row[0]),
            "run_id": str(row[1]),
            "run_revision": row[2],
            "coverage_revision": row[3],
            "revision_order": row[4],
            "row_type": row[5],
            "proposal_id": str(row[6]),
            "decision_id": str(row[7]),
            "outcome": row[8],
            "rejection_reasons": row[9] or [],
            "policy_version": row[10] or "",
            "scope_expansion_type": row[11],
            "scope_expansion_rationale": row[12],
            "scope_expansion_approved": row[13],
            "authorized_by": row[14] or "",
            "idempotency_key": row[15],
            "actor_type": row[16] or "system",
            "actor_identifier": row[17],
            "created_at": row[18],
        }

    def _get_proposal_row(self, cur, run_id, proposal_id):
        """Fetch an existing proposal row."""
        cur.execute(
            """SELECT id, run_id, run_revision, coverage_revision,
                revision_order, row_type,
                proposal_id, decision_type,
                target_coverage_item_ids,
                proposed_queries,
                proposed_candidate_ids,
                proposed_retrieval_queries,
                expected_contribution,
                estimated_cost,
                rationale,
                confidence,
                idempotency_key,
                actor_type,
                actor_identifier,
                created_at
            FROM strategy_revisions
            WHERE run_id=%s AND proposal_id=%s AND row_type='proposal'
            ORDER BY created_at DESC LIMIT 1""",
            (run_id, proposal_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_proposal_mapping(row)

    def _get_decision_row(self, cur, run_id, decision_id):
        """Fetch an existing decision row."""
        cur.execute(
            """SELECT id, run_id, run_revision, coverage_revision,
                revision_order, row_type,
                proposal_id, decision_id,
                outcome,
                rejection_reasons,
                policy_version,
                scope_expansion_type,
                scope_expansion_rationale,
                scope_expansion_approved,
                authorized_by,
                idempotency_key,
                actor_type,
                actor_identifier,
                created_at
            FROM strategy_revisions
            WHERE run_id=%s AND decision_id=%s AND row_type='decision'
            ORDER BY created_at DESC LIMIT 1""",
            (run_id, decision_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_decision_mapping(row)
