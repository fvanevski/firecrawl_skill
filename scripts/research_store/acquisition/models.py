"""Stable acquisition models shared by application policy and adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..domain import SearchAdapterResult, utcnow

DIRECT_SCRAPE_SUPPORTED_FORMATS = frozenset(
    {"markdown", "html", "rawHtml", "json", "links", "images", "summary"}
)
DIRECT_SCRAPE_FORMAT_MIME_TYPES: Mapping[str, str] = {
    "markdown": "text/markdown",
    "html": "text/html",
    "rawHtml": "text/html",
    "json": "application/json",
    "links": "application/json",
    "images": "application/json",
    "summary": "text/plain",
}


def _base_mime_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class AcquisitionResult:
    search_response_id: UUID
    run_id: UUID
    query_text: str
    backend: str
    status: str
    candidate_count: int
    candidates: list[dict[str, Any]]
    postgres_committed: bool
    event_id: UUID | None = None
    search_response: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False
    invocation_id: UUID | None = None
    attempt_ordinal: int | None = None


@dataclass(frozen=True)
class DirectScrapeRequest:
    """One direct URL or stable candidate-ID scrape request."""

    url: str | None = None
    candidate_id: UUID | str | None = None
    format: str = "markdown"
    summary: bool = False
    schema: Mapping[str, Any] | None = None
    mime_type: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.url is None) == (self.candidate_id is None):
            raise ValueError("provide exactly one of url or candidate_id")
        if self.url is not None and not self.url.strip():
            raise ValueError("url must be non-empty")
        if self.format not in DIRECT_SCRAPE_SUPPORTED_FORMATS:
            raise ValueError(f"unsupported scrape format: {self.format}")
        if self.schema is not None and not isinstance(self.schema, Mapping):
            raise TypeError("schema must be a mapping")
        if self.schema is not None and self.summary:
            raise ValueError("schema extraction cannot be combined with summary")
        if self.schema is not None and self.format not in {"markdown", "json"}:
            raise ValueError("schema extraction cannot be combined with another format")
        if self.summary and self.format not in {"markdown", "summary"}:
            raise ValueError("summary cannot be combined with another format")
        if self.mime_type is not None and not self.mime_type.strip():
            raise ValueError("mime_type must be non-empty when provided")
        if self.mime_type is not None:
            expected = DIRECT_SCRAPE_FORMAT_MIME_TYPES[self.effective_format]
            if _base_mime_type(self.mime_type) != _base_mime_type(expected):
                raise ValueError(
                    f"mime_type {self.mime_type!r} is incompatible with "
                    f"format {self.effective_format!r}"
                )

    @property
    def effective_summary(self) -> bool:
        return self.summary or self.format == "summary"

    @property
    def effective_format(self) -> str:
        if self.schema is not None:
            return "json"
        if self.effective_summary:
            return "summary"
        return self.format

    @property
    def effective_mime_type(self) -> str:
        return self.mime_type or DIRECT_SCRAPE_FORMAT_MIME_TYPES[self.effective_format]


@dataclass(frozen=True)
class ScrapeTransportResult:
    """In-memory direct-scrape transport result."""

    raw_payload: bytes
    returncode: int = 0
    stderr: bytes = b""
    requested_at: datetime = field(default_factory=utcnow)
    responded_at: datetime = field(default_factory=utcnow)
    http_status: int | None = None
    final_url: str | None = None
    title: str | None = None
    provider_request_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and bool(self.raw_payload)


@dataclass(frozen=True)
class DirectScrapeItemResult:
    index: int
    item_key: str
    status: str
    requested_url: str
    canonical_url: str
    candidate_id: UUID
    invocation_id: UUID
    format: str
    mime_type: str
    extraction_attempt_id: UUID | None = None
    source_id: UUID | None = None
    snapshot_id: UUID | None = None
    document_id: UUID | None = None
    derivation_id: UUID | None = None
    chunk_ids: tuple[UUID, ...] = ()
    content_sha256: str | None = None
    raw_blob_sha256: str | None = None
    reused_snapshot: bool = False
    reused_document: bool = False
    reused_chunks: bool = False
    error: str | None = None
    diagnostic: str | None = None
    failure_class: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DirectScrapeItemResult:
        uuid_fields = {
            "candidate_id",
            "invocation_id",
            "extraction_attempt_id",
            "source_id",
            "snapshot_id",
            "document_id",
            "derivation_id",
        }
        data = dict(value)
        for name in uuid_fields:
            if data.get(name) is not None and not isinstance(data[name], UUID):
                data[name] = UUID(str(data[name]))
        data["chunk_ids"] = tuple(UUID(str(item)) for item in data.get("chunk_ids", ()))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in (
            "candidate_id",
            "invocation_id",
            "extraction_attempt_id",
            "source_id",
            "snapshot_id",
            "document_id",
            "derivation_id",
        ):
            if value[name] is not None:
                value[name] = str(value[name])
        value["chunk_ids"] = [str(item) for item in self.chunk_ids]
        return value


@dataclass(frozen=True)
class DirectScrapeBatchResult:
    run_id: UUID
    invocation_id: UUID
    idempotency_key: str
    status: str
    items: tuple[DirectScrapeItemResult, ...]
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "direct-scrape-v1",
            "run_id": str(self.run_id),
            "invocation_id": str(self.invocation_id),
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "replayed": self.replayed,
            "items": [item.to_dict() for item in self.items],
        }


__all__ = [
    "DIRECT_SCRAPE_FORMAT_MIME_TYPES",
    "DIRECT_SCRAPE_SUPPORTED_FORMATS",
    "AcquisitionResult",
    "DirectScrapeBatchResult",
    "DirectScrapeItemResult",
    "DirectScrapeRequest",
    "ScrapeTransportResult",
    "SearchAdapterResult",
]
