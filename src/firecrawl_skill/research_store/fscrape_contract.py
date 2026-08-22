"""Public request, result, and error contracts for authoritative fscrape."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from firecrawl_skill.research_store.acquisition.models import (
    DirectScrapeBatchResult,
    DirectScrapeRequest,
)

MAX_DIAGNOSTIC_CHARS = 500
MAX_OUTPUT_ITEMS = 100
MAX_OUTPUT_IDS = 200
MAX_ITEM_IDS = 50
SUPPORTED_FORMATS = (
    "markdown",
    "html",
    "rawHtml",
    "json",
    "links",
    "images",
    "summary",
)
_RUN_ID_PATTERN = re.compile(
    r"^fr_(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_INVOCATION_ID_PATTERN = re.compile(r"^fc_[0-9a-f]{32}$")
_FAILURE_STAGES = frozenset({"preflight", "extraction", "ingestion", "indexing"})


class FScrapeArgumentError(ValueError):
    """A machine-renderable CLI argument failure."""


class FScrapeError(RuntimeError):
    """An authoritative fscrape failure with a stable stage label."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        result: FScrapeResult | None = None,
    ) -> None:
        if stage not in _FAILURE_STAGES:
            raise ValueError(f"unknown fscrape failure stage: {stage}")
        super().__init__(message)
        self.stage = stage
        self.result = result

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "authoritative-fscrape-error-v1",
            "status": "failed",
            "failure_stage": self.stage,
            "error": bounded_text(str(self)),
        }
        if self.result is not None:
            value["result"] = self.result.to_dict()
        return value


@dataclass(frozen=True)
class FScrapeRequest:
    urls: tuple[str, ...]
    research_run_id: str
    format: str = "markdown"
    summary: bool = False
    schema: Mapping[str, Any] | None = None
    idempotency_key: str | None = None
    external_invocation_id: str | None = None
    fresh: bool = False

    def __post_init__(self) -> None:
        if not self.urls or any(not url.strip() for url in self.urls):
            raise ValueError("at least one non-empty URL is required")
        validate_research_run_id(self.research_run_id)
        if self.format not in SUPPORTED_FORMATS:
            raise ValueError(f"unsupported scrape format: {self.format}")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        if self.external_invocation_id is not None:
            validate_invocation_id(self.external_invocation_id)
        if self.schema is not None:
            validate_schema(self.schema)
        self._direct_request(self.urls[0])

    def direct_requests(self) -> tuple[DirectScrapeRequest, ...]:
        return tuple(self._direct_request(url) for url in self.urls)

    def _direct_request(self, url: str) -> DirectScrapeRequest:
        return DirectScrapeRequest(
            url=url,
            format=self.format,
            summary=self.summary,
            schema=self.schema,
        )


@dataclass(frozen=True)
class FScrapeResult:
    research_run_id: str
    external_invocation_id: str
    batch: DirectScrapeBatchResult
    index_job_ids_by_chunk: Mapping[UUID, tuple[UUID, ...]]

    @property
    def status(self) -> str:
        return self.batch.status

    def to_dict(self) -> dict[str, Any]:
        selected = self.batch.items[:MAX_OUTPUT_ITEMS]
        corpus = {
            "source_ids": unique_ids(item.source_id for item in self.batch.items),
            "snapshot_ids": unique_ids(item.snapshot_id for item in self.batch.items),
            "document_ids": unique_ids(item.document_id for item in self.batch.items),
            "derivation_ids": unique_ids(
                item.derivation_id for item in self.batch.items
            ),
            "chunk_ids": unique_ids(
                chunk for item in self.batch.items for chunk in item.chunk_ids
            ),
            "index_job_ids": unique_ids(
                job for jobs in self.index_job_ids_by_chunk.values() for job in jobs
            ),
        }
        corpus_ids: dict[str, Any] = {}
        for name, values in corpus.items():
            bounded, truncated = bounded_strings(values, MAX_OUTPUT_IDS)
            corpus_ids[name] = bounded
            corpus_ids[f"{name[:-4]}_count"] = len(values)
            corpus_ids[f"{name}_truncated"] = truncated
        return {
            "schema_version": "authoritative-fscrape-v1",
            "status": self.batch.status,
            "run_id": str(self.batch.run_id),
            "research_run_id": self.research_run_id,
            "batch_id": str(self.batch.invocation_id),
            "invocation_id": str(self.batch.invocation_id),
            "external_invocation_id": self.external_invocation_id,
            "idempotency_key": self.batch.idempotency_key,
            "replayed": self.batch.replayed,
            "items": [self._item(item) for item in selected],
            "item_count": len(self.batch.items),
            "items_truncated": len(self.batch.items) > len(selected),
            "corpus_ids": corpus_ids,
        }

    def _item(self, item: Any) -> dict[str, Any]:
        chunks, chunks_truncated = bounded_strings(item.chunk_ids, MAX_ITEM_IDS)
        jobs = unique_ids(
            job
            for chunk in item.chunk_ids
            for job in self.index_job_ids_by_chunk.get(UUID(str(chunk)), ())
        )
        job_ids, jobs_truncated = bounded_strings(jobs, MAX_ITEM_IDS)
        return {
            "index": item.index,
            "status": item.status,
            "requested_url": bounded_text(item.requested_url),
            "canonical_url": bounded_text(item.canonical_url),
            "candidate_id": uuid_text(item.candidate_id),
            "extraction_attempt_id": uuid_text(item.extraction_attempt_id),
            "source_id": uuid_text(item.source_id),
            "snapshot_id": uuid_text(item.snapshot_id),
            "document_id": uuid_text(item.document_id),
            "derivation_id": uuid_text(item.derivation_id),
            "chunk_ids": chunks,
            "chunk_id_count": len(item.chunk_ids),
            "chunk_ids_truncated": chunks_truncated,
            "index_job_ids": job_ids,
            "index_job_id_count": len(jobs),
            "index_job_ids_truncated": jobs_truncated,
            "format": item.format,
            "mime_type": item.mime_type,
            "content_sha256": item.content_sha256,
            "raw_blob_sha256": item.raw_blob_sha256,
            "reused_snapshot": item.reused_snapshot,
            "reused_document": item.reused_document,
            "reused_chunks": item.reused_chunks,
            "failure_class": item.failure_class,
            "error": bounded_text(item.error),
            "diagnostic": bounded_text(item.diagnostic),
        }


def validate_research_run_id(value: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(value or ""):
        raise ValueError(
            "research run ID must match fr_<32 lowercase hexadecimal characters> "
            "or fr_<canonical lowercase UUID>"
        )
    return value


def validate_invocation_id(value: str) -> str:
    if not _INVOCATION_ID_PATTERN.fullmatch(value or ""):
        raise ValueError(
            "invocation ID must match fc_<32 lowercase hexadecimal characters>"
        )
    return value


def new_invocation_id() -> str:
    return f"fc_{uuid4().hex}"


def validate_schema(schema: Mapping[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise FScrapeArgumentError(f"invalid JSON schema: {exc.message}") from exc


def bounded_strings(values: Any, limit: int) -> tuple[list[str], bool]:
    normalized = [str(value) for value in values if value is not None]
    return normalized[:limit], len(normalized) > limit


def bounded_text(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)[:MAX_DIAGNOSTIC_CHARS]


def uuid_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def unique_ids(values: Any) -> list[UUID]:
    return list(dict.fromkeys(value for value in values if value is not None))
