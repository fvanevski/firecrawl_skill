from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, BinaryIO, Protocol
from uuid import UUID

from .domain import BlobReference, IngestRequest, IngestResult


class SourceRepository(Protocol):
    def upsert_source(self, canonical_url: str, metadata: dict[str, Any]) -> UUID: ...


class SnapshotRepository(Protocol):
    def persist_ingest(
        self,
        request: IngestRequest,
        canonical_url: str,
        blob: BlobReference,
        normalized_text: str,
        blocks: list[Any],
        chunks: list[Any],
        parser_version: str,
        chunker_version: str,
    ) -> IngestResult: ...


class DocumentRepository(Protocol):
    def inspect_asset(self, candidate_id: UUID) -> dict[str, Any]: ...
    def fetch_passages(
        self,
        candidate_ids: list[UUID],
        max_tokens: int,
        max_passages: int,
        include_neighbors: bool,
    ) -> list[dict[str, Any]]: ...


class ChunkRepository(Protocol):
    def chunks_for_index(
        self, chunk_ids: list[UUID] | None = None
    ) -> list[dict[str, Any]]: ...


class ResearchRunRepository(Protocol):
    def start_run(self, original_request: str, metadata: dict[str, Any]) -> UUID: ...


class RetrievalEventRepository(Protocol):
    def log_retrieval(self, run_id: UUID, event: dict[str, Any]) -> None: ...


class IndexJobRepository(Protocol):
    def claim_jobs(self, limit: int) -> list[dict[str, Any]]: ...
    def finish_job(self, job_id: UUID, error: str | None = None) -> None: ...


class BlobStore(Protocol):
    def put(self, stream: BinaryIO, mime_type: str | None = None) -> BlobReference: ...
    def open(self, digest: str) -> BinaryIO: ...
    def exists(self, digest: str) -> bool: ...
    def verify(self, digest: str) -> bool: ...


class RetrievalIndex(Protocol):
    def ensure_schema(self) -> None: ...
    def upsert(self, points: list[dict[str, Any]]) -> None: ...
    def search(
        self, vector: list[float], filters: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]: ...
    def delete(self, ids: list[UUID]) -> None: ...


class QueueBackend(Protocol):
    def notify(self, job_id: UUID, ttl_seconds: int = 3600) -> None: ...


class UnitOfWork(AbstractContextManager, Protocol):
    sources: SourceRepository
    snapshots: SnapshotRepository
    documents: DocumentRepository
    chunks: ChunkRepository
    runs: ResearchRunRepository
    retrieval_events: RetrievalEventRepository
    index_jobs: IndexJobRepository

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
