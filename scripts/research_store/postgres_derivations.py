"""Connection-bound PostgreSQL derivation repository for issue #256."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .domain import DerivationAttempt


class PostgresDerivationRepository:
    """Canonical derivation persistence using only the UoW-owned connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def list_all_targets(self) -> list[dict]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.id AS document_id,
                       a.id AS snapshot_id,
                       d.parser_version,
                       d.normalization_version,
                       dd.configuration_sha256
                FROM documents d
                JOIN asset_snapshots a ON a.id = d.snapshot_id
                LEFT JOIN document_derivations dd
                  ON dd.document_id = d.id
                  AND dd.status IN ('pending', 'active')
                ORDER BY d.id, a.id
                """
            )
            keys = (
                "document_id",
                "snapshot_id",
                "parser_version",
                "normalization_version",
                "configuration_sha256",
            )
            result = []
            for row in cur.fetchall():
                item = dict(zip(keys, row))
                item["document_id"] = str(item["document_id"])
                item["snapshot_id"] = str(item["snapshot_id"])
                result.append(item)
            return result

    def get_document_for_snapshot(self, snapshot_id: UUID) -> list[dict]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.id AS document_id,
                       d.parser_version,
                       d.normalization_version,
                       dd.configuration_sha256
                FROM documents d
                JOIN asset_snapshots a ON a.id = d.snapshot_id
                LEFT JOIN document_derivations dd
                  ON dd.document_id = d.id
                  AND dd.status IN ('pending', 'active')
                WHERE a.id = %s
                ORDER BY d.id
                """,
                (str(snapshot_id),),
            )
            keys = (
                "document_id",
                "parser_version",
                "normalization_version",
                "configuration_sha256",
            )
            result = []
            for row in cur.fetchall():
                item = dict(zip(keys, row))
                item["document_id"] = str(item["document_id"])
                result.append(item)
            return result

    def get_snapshots_for_document(self, document_id: UUID) -> list[dict]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT a.id AS snapshot_id,
                       d.parser_version,
                       d.normalization_version,
                       dd.configuration_sha256
                FROM documents d
                JOIN asset_snapshots a ON a.id = d.snapshot_id
                LEFT JOIN document_derivations dd
                  ON dd.document_id = d.id
                  AND dd.status IN ('pending', 'active')
                WHERE d.id = %s
                ORDER BY a.id
                """,
                (str(document_id),),
            )
            keys = (
                "snapshot_id",
                "parser_version",
                "normalization_version",
                "configuration_sha256",
            )
            result = []
            for row in cur.fetchall():
                item = dict(zip(keys, row))
                item["snapshot_id"] = str(item["snapshot_id"])
                result.append(item)
            return result

    def get_snapshot_info(self, snapshot_id: UUID) -> dict | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT a.content_sha256,
                       a.mime_type,
                       d.title,
                       a.requested_url,
                       a.final_url,
                       a.retrieved_at,
                       a.http_status,
                       a.extraction_attempt_id
                FROM asset_snapshots a
                JOIN documents d ON d.snapshot_id = a.id
                WHERE a.id = %s
                ORDER BY d.id DESC
                LIMIT 1
                """,
                (str(snapshot_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            keys = (
                "content_sha256",
                "mime_type",
                "title",
                "requested_url",
                "final_url",
                "retrieved_at",
                "http_status",
                "extraction_attempt_id",
            )
            item = dict(zip(keys, row))
            if item.get("extraction_attempt_id") is not None:
                item["extraction_attempt_id"] = str(item["extraction_attempt_id"])
            return item

    def find_by_configuration(
        self, document_id: UUID, configuration_sha256: str
    ) -> dict | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, parser_version, normalization_version,
                       chunker_name, chunker_version, tokenizer_name
                FROM document_derivations
                WHERE document_id = %s
                  AND configuration_sha256 = %s
                  AND status IN ('pending', 'active')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(document_id), configuration_sha256),
            )
            row = cur.fetchone()
            if row is None:
                return None
            keys = (
                "id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
            )
            return dict(zip(keys, row))

    def activate(self, derivation_id: UUID) -> DerivationAttempt:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, snapshot_id, status,
                  parser_version, normalization_version,
                  chunker_name, chunker_version, tokenizer_name,
                  chunk_count, block_count, configuration_sha256,
                  error_message, created_at
                FROM document_derivations
                WHERE id = %s
                FOR UPDATE
                """,
                (str(derivation_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"derivation not found: {derivation_id}")

            keys = (
                "id",
                "document_id",
                "snapshot_id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "chunk_count",
                "block_count",
                "configuration_sha256",
                "error_message",
                "created_at",
            )
            current = dict(zip(keys, row))
            if current["status"] != "pending":
                raise ValueError(
                    f"derivation {derivation_id} is not pending "
                    f"(status: {current['status']})"
                )

            cur.execute(
                """
                UPDATE document_derivations
                SET status = 'superseded'
                WHERE document_id = %s
                  AND status = 'active'
                  AND id != %s
                """,
                (current["document_id"], str(derivation_id)),
            )
            cur.execute(
                """
                UPDATE document_derivations
                SET status = 'active'
                WHERE id = %s
                RETURNING id, document_id, snapshot_id, status,
                  parser_version, normalization_version,
                  chunker_name, chunker_version, tokenizer_name,
                  chunk_count, block_count, configuration_sha256,
                  error_message, created_at
                """,
                (str(derivation_id),),
            )
            return DerivationAttempt.from_mapping(dict(zip(keys, cur.fetchone())))

    def count_chunks_for_derivation(self, derivation_id: UUID) -> int | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_count, snapshot_id
                FROM document_derivations
                WHERE id = %s
                """,
                (str(derivation_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row[0] is not None:
                return row[0]
            cur.execute(
                """
                SELECT count(*) FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.snapshot_id = %s
                """,
                (str(row[1]),),
            )
            return cur.fetchone()[0]

    def count_blocks_for_derivation(self, derivation_id: UUID) -> int | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT block_count, snapshot_id
                FROM document_derivations
                WHERE id = %s
                """,
                (str(derivation_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row[0] is not None:
                return row[0]
            cur.execute(
                """
                SELECT count(*) FROM document_blocks db
                JOIN documents d ON d.id = db.document_id
                WHERE d.snapshot_id = %s
                """,
                (str(row[1]),),
            )
            return cur.fetchone()[0]

    def list(
        self,
        document_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        status: str | None = None,
    ) -> list[dict]:
        conditions = []
        params: list[Any] = []
        if document_id is not None:
            conditions.append("document_id = %s")
            params.append(str(document_id))
        if snapshot_id is not None:
            conditions.append("snapshot_id = %s")
            params.append(str(snapshot_id))
        if status is not None:
            conditions.append("status = %s")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""
            SELECT id, document_id, snapshot_id, status,
                   parser_version, normalization_version,
                   chunker_name, chunker_version, tokenizer_name,
                   chunk_count, block_count, configuration_sha256,
                   error_message, created_at
            FROM document_derivations
            {where}
            ORDER BY created_at DESC
        """
        with self.__connection.cursor() as cur:
            cur.execute(query, params)
            keys = [
                "id",
                "document_id",
                "snapshot_id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "chunk_count",
                "block_count",
                "configuration_sha256",
                "error_message",
                "created_at",
            ]
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def get(self, derivation_id: UUID) -> dict | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, snapshot_id, status,
                       parser_version, normalization_version,
                       chunker_name, chunker_version, tokenizer_name,
                       chunk_count, block_count, configuration_sha256,
                       error_message, created_at
                FROM document_derivations
                WHERE id = %s
                """,
                (str(derivation_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            keys = [
                "id",
                "document_id",
                "snapshot_id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "chunk_count",
                "block_count",
                "configuration_sha256",
                "error_message",
                "created_at",
            ]
            return dict(zip(keys, row))

    def create(
        self,
        document_id: UUID,
        snapshot_id: UUID,
        parser_version: str,
        normalization_version: str,
        chunker_name: str,
        chunker_version: str,
        tokenizer_name: str,
        chunk_count: int | None = None,
        block_count: int | None = None,
        configuration_sha256: str | None = None,
        status: str = "pending",
        error_message: str | None = None,
    ) -> DerivationAttempt:
        with self.__connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_derivations (
                    document_id, snapshot_id, status,
                    parser_version, normalization_version,
                    chunker_name, chunker_version, tokenizer_name,
                    chunk_count, block_count, configuration_sha256,
                    error_message
                ) VALUES (
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s
                ) RETURNING id, document_id, snapshot_id, status,
                            parser_version, normalization_version,
                            chunker_name, chunker_version, tokenizer_name,
                            chunk_count, block_count, configuration_sha256,
                            error_message, created_at
                """,
                (
                    str(document_id),
                    str(snapshot_id),
                    status,
                    parser_version,
                    normalization_version,
                    chunker_name,
                    chunker_version,
                    tokenizer_name,
                    chunk_count,
                    block_count,
                    configuration_sha256,
                    error_message,
                ),
            )
            keys = [
                "id",
                "document_id",
                "snapshot_id",
                "status",
                "parser_version",
                "normalization_version",
                "chunker_name",
                "chunker_version",
                "tokenizer_name",
                "chunk_count",
                "block_count",
                "configuration_sha256",
                "error_message",
                "created_at",
            ]
            item = dict(zip(keys, cur.fetchone()))
            item["id"] = str(item["id"])
            item["document_id"] = str(item["document_id"])
            item["snapshot_id"] = str(item["snapshot_id"])
            return DerivationAttempt.from_mapping(item)
