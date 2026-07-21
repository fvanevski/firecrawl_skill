from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BlobReference:
    sha256: str
    uri: str
    byte_length: int
    mime_type: str | None = None


@dataclass(frozen=True)
class Block:
    ordinal: int
    block_type: str
    text: str
    heading_path: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    content_sha256: str
    first_block_ordinal: int
    last_block_ordinal: int
    token_count: int
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngestRequest:
    requested_url: str
    content: bytes
    normalized_content: bytes | None = None
    mime_type: str = "text/markdown"
    final_url: str | None = None
    title: str | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    published_at: datetime | None = None
    firecrawl_version: str | None = None
    crawl_options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    source_id: UUID
    snapshot_id: UUID
    document_id: UUID
    chunk_ids: tuple[UUID, ...]
    content_sha256: str
    reused_snapshot: bool
    reused_document: bool = False
    reused_chunks: bool = False


@dataclass(frozen=True)
class IndexDefinition:
    id: UUID
    fingerprint: str
    physical_collection: str
    model_name: str
    model_revision: str
    dimension: int
    distance_metric: str = "Cosine"
    normalization: str = ""
    instruction_template_hash: str = ""


@dataclass(frozen=True)
class RawSearchResponse:
    id: UUID
    run_id: UUID
    query_text: str
    backend: str
    status: str
    parser_version: str
    raw_blob: BlobReference
    content_sha256: str
    idempotency_key: str
    plan_id: UUID | None = None
    plan_query_id: UUID | None = None
    provider_request_id: str | None = None
    http_status: int | None = None
    result_count: int = 0
    error_message: str | None = None
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    payload_summary: dict[str, Any] = field(default_factory=dict)
    requested_at: datetime = field(default_factory=utcnow)
    responded_at: datetime = field(default_factory=utcnow)
    created_at: datetime = field(default_factory=utcnow)


def new_id() -> UUID:
    return uuid4()
