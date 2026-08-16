"""Connection-bound PostgreSQL corpus persistence for Phase-3 issue #256.

The repository receives the exact connection opened by PostgresUnitOfWork.  It
does not own connection lifecycle or transaction control.  Corpus ingestion,
batch membership/finalization, and run-asset linkage therefore participate in
the caller's existing outer transaction and savepoints.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from .domain import BlobReference, IngestRequest, IngestResult
from .ingestion_batch_semantics import (
    _export_invocation,
    _export_invocation_by_batch,
    _finish_ingestion_batch,
    _record_batch_asset,
    _start_ingestion_batch,
)


class _BatchPersistenceAdapter:
    """Private compatibility target for the authoritative issue #217 functions."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _has_sealed_at_column(connection):
        with connection.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM information_schema.columns
                WHERE table_name='ingestion_batches'
                  AND column_name='sealed_at'
                LIMIT 1"""
            )
            return cur.fetchone() is not None

    @staticmethod
    def _has_extraction_attempt_id_column(connection):
        with connection.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM information_schema.columns
                WHERE table_name='ingestion_batch_assets'
                  AND column_name='extraction_attempt_id'
                LIMIT 1"""
            )
            return cur.fetchone() is not None


class PostgresCorpusRepository:
    """Canonical corpus/asset persistence bound to one UoW-owned connection."""

    def __init__(
        self,
        connection: Any,
        *,
        embedding_model: str,
        embedding_revision: str,
        embedding_dimension: int,
        indexing_persistence_error: type[Exception],
    ) -> None:
        self.__connection = connection
        self.__batch_adapter = _BatchPersistenceAdapter(connection)
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_dimension = embedding_dimension
        self._indexing_persistence_error = indexing_persistence_error

    def upsert_source(self, canonical_url: str, metadata: dict[str, Any]):
        """Upsert one canonical source using the same row-locking conflict path."""
        domain = urlsplit(canonical_url).hostname
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO sources(canonical_url, registered_domain, metadata)
                VALUES (%s,%s,%s) ON CONFLICT(canonical_url) DO UPDATE
                SET last_seen_at=now(), metadata=sources.metadata || excluded.metadata
                RETURNING id""",
                (canonical_url, domain, json.dumps(metadata)),
            )
            return cur.fetchone()[0]

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
        chunker_name: str = "structural",
        parser_name: str = "markdown",
    ) -> IngestResult:
        domain = urlsplit(canonical_url).hostname
        document_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
        with self.__connection.cursor() as cur:
            # The conflict update takes a row lock. All snapshot decisions for
            # one canonical source are serialized before the unique
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
                    content_sha256,raw_blob_uri,raw_byte_length,firecrawl_version,crawl_options,parent_snapshot_id,extraction_attempt_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
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
                        request.extraction_attempt_id,
                    ),
                )
                snapshot_id = cur.fetchone()[0]

            cur.execute(
                """SELECT id FROM documents
                WHERE snapshot_id=%s AND parser_name=%s AND parser_version=%s
                  AND normalization_version=%s AND document_sha256=%s""",
                (
                    snapshot_id,
                    parser_name,
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
                    parser_version,normalization_version,document_sha256,metadata,extraction_attempt_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        snapshot_id,
                        request.title,
                        request.published_at,
                        normalized_text,
                        normalized_text,
                        parser_name,
                        parser_version,
                        normalization_version,
                        document_hash,
                        json.dumps(request.metadata),
                        request.extraction_attempt_id,
                    ),
                )
                document_id = cur.fetchone()[0]
                block_ids = {}
                for block in blocks:
                    cur.execute(
                        """INSERT INTO document_blocks(document_id,block_type,heading_path,ordinal,char_start,char_end,text,metadata,parser_version)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (
                            document_id,
                            block.block_type,
                            list(block.heading_path),
                            block.ordinal,
                            block.char_start,
                            block.char_end,
                            block.text,
                            json.dumps(block.metadata),
                            parser_version,
                        ),
                    )
                    block_ids[block.ordinal] = cur.fetchone()[0]

            tokenizer_name = chunks[0].tokenizer_name if chunks else None

            if tokenizer_name is not None:
                cur.execute(
                    """SELECT id FROM chunks WHERE document_id=%s
                    AND chunker_name=%s AND chunker_version=%s AND tokenizer_name=%s ORDER BY ordinal""",
                    (document_id, chunker_name, chunker_version, tokenizer_name),
                )
            else:
                cur.execute(
                    """SELECT id FROM chunks WHERE document_id=%s
                    AND chunker_name=%s AND chunker_version=%s AND tokenizer_name IS NULL ORDER BY ordinal""",
                    (document_id, chunker_name, chunker_version),
                )
            chunk_ids = [row[0] for row in cur.fetchall()]
            reused_chunks = bool(chunk_ids)
            if not chunk_ids:
                for chunk in chunks:
                    metadata_dict: dict[str, object] = {
                        "heading_path": list(chunk.heading_path),
                    }
                    if chunk.tokenizer_name is not None:
                        metadata_dict["tokenizer_name"] = chunk.tokenizer_name
                    if chunk.parent_block_ordinal is not None:
                        metadata_dict["parent_block_ordinal"] = (
                            chunk.parent_block_ordinal
                        )

                    cur.execute(
                        """INSERT INTO chunks(document_id,first_block_id,last_block_id,ordinal,text,token_count,content_sha256,
                        chunker_name,chunker_version,tokenizer_name,parent_block_id,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (
                            document_id,
                            block_ids.get(chunk.first_block_ordinal),
                            block_ids.get(chunk.last_block_ordinal),
                            chunk.ordinal,
                            chunk.text,
                            chunk.token_count,
                            chunk.content_sha256,
                            chunker_name,
                            chunker_version,
                            chunk.tokenizer_name,
                            block_ids.get(chunk.parent_block_ordinal)
                            if chunk.parent_block_ordinal is not None
                            else None,
                            json.dumps(metadata_dict),
                        ),
                    )
                    chunk_ids.append(cur.fetchone()[0])

            try:
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
            except Exception as exc:
                raise self._indexing_persistence_error(
                    f"index manifest/job persistence failed: {exc}"
                ) from exc
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
        with self.__connection.cursor() as cur:
            return self._ensure_index_definition(cur)

    def link_run_asset(
        self, external_run_id, snapshot_id, role="acquired", metadata=None
    ):
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                SELECT id,%s,%s,%s FROM research_runs
                WHERE external_run_id=%s AND state NOT IN ('completed','partial','failed','cancelled')
                ON CONFLICT(run_id,snapshot_id,role) DO UPDATE
                SET metadata=research_run_assets.metadata || excluded.metadata""",
                (snapshot_id, role, json.dumps(metadata or {}), external_run_id),
            )
            if cur.rowcount != 1:
                raise KeyError(external_run_id)

    # Issue #217 remains the active batch-semantics authority. These thin
    # wrappers execute its exact functions against a private adapter carrying
    # the UoW-owned connection. The public repository object therefore does
    # not expose the raw connection or transaction controls.
    def start_ingestion_batch(self, *args, **kwargs):
        return _start_ingestion_batch(self.__batch_adapter, *args, **kwargs)

    def record_batch_asset(self, *args, **kwargs):
        return _record_batch_asset(self.__batch_adapter, *args, **kwargs)

    def finish_ingestion_batch(self, *args, **kwargs):
        return _finish_ingestion_batch(self.__batch_adapter, *args, **kwargs)

    def export_invocation(self, *args, **kwargs):
        return _export_invocation(self.__batch_adapter, *args, **kwargs)

    def export_invocation_by_batch(self, *args, **kwargs):
        return _export_invocation_by_batch(self.__batch_adapter, *args, **kwargs)
