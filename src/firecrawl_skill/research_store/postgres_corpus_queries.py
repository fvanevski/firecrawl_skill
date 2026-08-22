"""Connection-bound PostgreSQL corpus query persistence for issue #259.

The repository receives the exact connection owned by ``PostgresUnitOfWork``.
It never opens, commits, rolls back, closes, or creates savepoints on that
connection.  These query methods were the residual corpus/index-read methods
left on the legacy UoW after issues #255-#258.
"""

from __future__ import annotations

from typing import Any


class PostgresCorpusQueryRepository:
    """Canonical corpus inspection, retrieval, and index-read persistence."""

    def __init__(
        self,
        connection: Any,
        *,
        parser_version: str,
        normalization_version: str,
        chunker_version: str,
    ) -> None:
        self.__connection = connection
        self.__parser_version = parser_version
        self.__normalization_version = normalization_version
        self.__chunker_version = chunker_version

    def corpus_overview(self):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT (SELECT count(*) FROM sources),
                (SELECT count(*) FROM asset_snapshots),
                (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks),
                (SELECT min(retrieved_at) FROM asset_snapshots),
                (SELECT max(retrieved_at) FROM asset_snapshots)"""
            )
            row = cur.fetchone()
            cur.execute(
                "SELECT registered_domain,count(*) FROM sources "
                "GROUP BY registered_domain ORDER BY count(*) DESC LIMIT 50"
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
                "SELECT qdrant_collection,model_name,model_revision,dimension,count(*) "
                "FROM embedding_manifests GROUP BY 1,2,3,4"
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
            cur.execute("SELECT state,count(*) FROM research_runs GROUP BY state")
            overview["research_runs"] = dict(cur.fetchall())
            return overview

    def search_lexical(self, query: str, limit: int, filters: dict):
        domain = filters.get("domain")
        source_type = filters.get("source_type")
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,d.title,s.registered_domain,d.published_at,
                c.metadata->'heading_path',left(c.text,400),
                ts_rank_cd(c.search_vector,websearch_to_tsquery('simple',%s)) score,
                a.id,s.id,s.canonical_url,a.retrieved_at
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id
                WHERE c.search_vector @@ websearch_to_tsquery('simple',%s)
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
                    self.__parser_version,
                    self.__normalization_version,
                    self.__chunker_version,
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
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.document_id,d.snapshot_id,a.source_id,
                s.canonical_url,a.retrieved_at,d.title,c.ordinal,
                c.metadata->'heading_path',c.token_count,a.content_sha256,
                a.parent_snapshot_id
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id
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
                "SELECT ordinal,block_type,heading_path,left(text,160) "
                "FROM document_blocks WHERE document_id=%s ORDER BY ordinal",
                (row[1],),
            )
            result["outline"] = [
                {
                    "ordinal": r[0],
                    "type": r[1],
                    "heading_path": r[2],
                    "preview": r[3],
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT id,retrieved_at,content_sha256,parent_snapshot_id "
                "FROM asset_snapshots WHERE source_id=%s ORDER BY retrieved_at",
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
                "SELECT id,ordinal,metadata->'heading_path' FROM chunks "
                "WHERE document_id=%s AND ordinal BETWEEN %s AND %s ORDER BY ordinal",
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
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.document_id,c.ordinal,c.text,c.token_count,
                c.metadata->'heading_path',d.snapshot_id,a.source_id,
                s.canonical_url,a.retrieved_at
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id
                WHERE c.id=ANY(%s) ORDER BY array_position(%s::uuid[],c.id)""",
                (candidate_ids, candidate_ids),
            )
            passages, used = [], 0
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
            for row in cur.fetchall():
                if len(passages) >= max_passages or used + row[4] > max_tokens:
                    break
                passages.append(dict(zip(keys, row)))
                used += row[4]
            return passages

    def fetch_run_passages(self, run_id, chunk_ids, max_tokens, max_passages):
        """Fetch only chunks linked to the exact research run."""
        if not chunk_ids or max_tokens <= 0 or max_passages <= 0:
            return []
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.document_id,c.ordinal,c.text,c.token_count,
                c.metadata->'heading_path',d.snapshot_id,a.source_id,
                s.canonical_url,a.retrieved_at,d.published_at
                FROM chunks c
                JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id
                JOIN research_run_assets rra
                  ON rra.snapshot_id=d.snapshot_id AND rra.run_id=%s
                WHERE c.id=ANY(%s)
                ORDER BY array_position(%s::uuid[],c.id)""",
                (run_id, chunk_ids, chunk_ids),
            )
            passages, used = [], 0
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
                "published_at",
            )
            for row in cur.fetchall():
                if len(passages) >= max_passages or used + row[4] > max_tokens:
                    break
                passages.append(dict(zip(keys, row)))
                used += row[4]
            return passages

    def expand_relationships(self, candidate_ids, hops, max_results):
        with self.__connection.cursor() as cur:
            cur.execute(
                """WITH RECURSIVE walk AS (
                SELECT r.*,1 depth FROM relations r WHERE r.subject_id=ANY(%s)
                UNION ALL SELECT r.*,w.depth+1 FROM relations r
                JOIN walk w ON r.subject_id=w.object_id WHERE w.depth < %s)
                SELECT id,subject_type,subject_id,predicate,object_type,object_id,
                object_literal,relation_class,source_snapshot_id,source_block_id,
                supporting_span,confidence,depth FROM walk LIMIT %s""",
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

    def chunks_for_index(self, chunk_ids=None, manifest_id=None):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT c.id chunk_id,c.text,c.document_id,d.snapshot_id,
                a.source_id,s.registered_domain,s.source_type,s.canonical_url,
                d.title,c.metadata->'heading_path' heading_path,a.retrieved_at,
                d.published_at,d.language,c.content_sha256,s.default_authority_class,
                d.parser_version,d.normalization_version,c.chunker_version
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN asset_snapshots a ON a.id=d.snapshot_id
                JOIN sources s ON s.id=a.source_id
                WHERE (%s::uuid[] IS NULL OR c.id=ANY(%s))
                  AND (%s::uuid IS NULL OR EXISTS(
                    SELECT 1 FROM embedding_manifests em
                    WHERE em.chunk_id=c.id AND em.id=%s::uuid))""",
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
