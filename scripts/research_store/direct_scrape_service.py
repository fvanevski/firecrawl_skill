"""PostgreSQL-authoritative direct Firecrawl scrape ingestion.

The service performs the pre-network authority check before constructing or
invoking transport, captures provider bytes in memory, persists them in
``BLOB_ROOT``, and returns corpus identifiers only after the matching
PostgreSQL transaction commits.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .acquisition_authority import (
    ACQUISITION_ENTRY_STATES,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .config import StoreConfig
from .derivation_service import _configuration_sha256
from .domain import IngestRequest, utcnow
from .postgres import IndexingPersistenceError
from .url import canonicalize_candidate_url

_SUPPORTED_FORMATS = frozenset(
    {"markdown", "html", "rawHtml", "json", "links", "images", "summary"}
)
_FORMAT_MIME_TYPES: Mapping[str, str] = {
    "markdown": "text/markdown",
    "html": "text/html",
    "rawHtml": "text/html",
    "json": "application/json",
    "links": "application/json",
    "images": "application/json",
    "summary": "text/plain",
}
_MAX_DIAGNOSTIC_CHARS = 500
_FIRECRAWL_ADAPTER_VERSION = "direct-v2"
_FIRECRAWL_STDOUT_CONTRACT = "single-format-raw-stdout-v1"


def _base_mime_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


DIRECT_SCRAPE_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "research_runs": frozenset({"SELECT", "UPDATE"}),
    "research_invocations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "research_events": frozenset({"SELECT", "INSERT"}),
    "search_candidates": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "extraction_attempts": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "sources": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "asset_snapshots": frozenset({"SELECT", "INSERT"}),
    "documents": frozenset({"SELECT", "INSERT"}),
    "document_blocks": frozenset({"SELECT", "INSERT"}),
    "chunks": frozenset({"SELECT", "INSERT"}),
    "index_definitions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "embedding_manifests": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "index_jobs": frozenset({"SELECT", "INSERT"}),
    "document_derivations": frozenset({"SELECT", "INSERT"}),
    "research_run_assets": frozenset({"SELECT", "INSERT", "UPDATE"}),
}


class DirectScrapeError(RuntimeError):
    """Base error for authoritative direct scrape execution."""


class DirectScrapePersistenceError(DirectScrapeError):
    """A typed authoritative persistence failure."""

    def __init__(self, message: str, *, stage: str = "ingestion") -> None:
        if stage not in {"ingestion", "indexing"}:
            raise ValueError(f"invalid direct-scrape persistence stage: {stage}")
        super().__init__(message)
        self.stage = stage


def require_direct_scrape_persistence(uow_factory: Callable[[], Any]) -> None:
    """Verify every table privilege used by direct scrape before transport."""
    missing: list[str] = []
    with uow_factory() as uow, uow.connection.cursor() as cur:
        for table, privileges in DIRECT_SCRAPE_TABLE_PRIVILEGES.items():
            for privilege in sorted(privileges):
                cur.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (table, privilege),
                )
                row = cur.fetchone()
                if not row or row[0] is not True:
                    missing.append(f"{table}:{privilege}")
    if missing:
        raise DirectScrapePersistenceError(
            "authoritative PostgreSQL role lacks direct-scrape privileges: "
            + ", ".join(missing)
        )


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
        if self.format not in _SUPPORTED_FORMATS:
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
            expected = _FORMAT_MIME_TYPES[self.effective_format]
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
        return self.mime_type or _FORMAT_MIME_TYPES[self.effective_format]


@dataclass(frozen=True)
class ScrapeTransportResult:
    """In-memory Firecrawl transport result."""

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


@dataclass(frozen=True)
class _ResolvedTarget:
    index: int
    item_key: str
    request: DirectScrapeRequest
    candidate_id: UUID
    requested_url: str
    canonical_url: str
    title: str | None
    retry_parent_id: UUID | None = None


class FirecrawlDirectScrapeAdapter:
    """Capture Firecrawl CLI output through pipes rather than output files."""

    def __init__(
        self,
        *,
        executable: str = "firecrawl",
        timeout_seconds: int = 60,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        version_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.version_runner = version_runner
        self._version_cache: str | None = None

    def scrape(
        self,
        url: str,
        *,
        format: str = "markdown",
        summary: bool = False,
        schema: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ScrapeTransportResult:
        if format not in _SUPPORTED_FORMATS:
            raise ValueError(f"unsupported scrape format: {format}")
        effective_format = (
            "json" if schema is not None else ("summary" if summary else format)
        )
        command = [self.executable, "scrape", url, "--format", effective_format]
        if schema is not None:
            command.extend(
                [
                    "--schema",
                    json.dumps(schema, sort_keys=True, separators=(",", ":")),
                ]
            )
        elif effective_format == "markdown":
            command.extend(
                [
                    "--only-main-content",
                    "--exclude-tags",
                    (
                        "nav,footer,aside,header,script,style,.sidebar,#sidebar,"
                        ".ad,.menu,#menu,.header,.footer,#header,#footer,#nav,.nav"
                    ),
                ]
            )
        self._append_options(command, options or {})

        requested_at = utcnow()
        try:
            process = self.runner(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = str(exc).encode("utf-8", errors="replace")
            return ScrapeTransportResult(
                raw_payload=exc.stdout or b"",
                returncode=124,
                stderr=stderr,
                requested_at=requested_at,
                responded_at=utcnow(),
                metadata={"failure_class": "timeout"},
            )
        except OSError as exc:
            return ScrapeTransportResult(
                raw_payload=b"",
                returncode=127,
                stderr=str(exc).encode("utf-8", errors="replace"),
                requested_at=requested_at,
                responded_at=utcnow(),
                metadata={"failure_class": "network"},
            )

        stdout = (
            process.stdout.encode()
            if isinstance(process.stdout, str)
            else process.stdout
        )
        stderr = (
            process.stderr.encode()
            if isinstance(process.stderr, str)
            else process.stderr
        )
        return ScrapeTransportResult(
            raw_payload=stdout or b"",
            returncode=int(process.returncode),
            stderr=stderr or b"",
            requested_at=requested_at,
            responded_at=utcnow(),
            metadata={
                "adapter": type(self).__name__,
                "adapter_version": _FIRECRAWL_ADAPTER_VERSION,
                "stdout_contract": _FIRECRAWL_STDOUT_CONTRACT,
                "firecrawl_cli_version": self._cli_version(),
                "command": self._sanitized_command(command),
                "request": {
                    "format": effective_format,
                    "schema": schema is not None,
                    "options": dict(options or {}),
                },
                "exit_code": int(process.returncode),
            },
        )

    def _cli_version(self) -> str | None:
        if self._version_cache is not None:
            return self._version_cache
        try:
            process = self.version_runner(
                [self.executable, "--version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            value = process.stdout
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            version = str(value or "").strip()[:100]
            self._version_cache = version or "unknown"
        except (OSError, subprocess.SubprocessError):
            self._version_cache = "unknown"
        return self._version_cache

    @staticmethod
    def _sanitized_command(command: Sequence[str]) -> list[str]:
        sanitized = list(command)
        if "--schema" in sanitized:
            index = sanitized.index("--schema") + 1
            if index < len(sanitized):
                digest = hashlib.sha256(sanitized[index].encode()).hexdigest()[:12]
                sanitized[index] = f"<schema-sha256:{digest}>"
        return sanitized

    @staticmethod
    def _append_options(command: list[str], options: Mapping[str, Any]) -> None:
        """Append a constrained set of transport options deterministically."""
        supported = {
            "include_tags": "--include-tags",
            "exclude_tags": "--exclude-tags",
            "wait_for": "--wait-for",
            "timeout": "--timeout",
            "mobile": "--mobile",
            "location": "--location",
        }
        unknown = sorted(set(options) - set(supported))
        if unknown:
            raise ValueError(f"unsupported Firecrawl options: {', '.join(unknown)}")
        for key in sorted(options):
            value = options[key]
            flag = supported[key]
            if isinstance(value, bool):
                if value:
                    command.append(flag)
            elif value is not None:
                command.extend([flag, str(value)])


class DirectScrapeService:
    """Execute direct scrapes with PostgreSQL/blob authority and resumable IDs."""

    def __init__(
        self,
        config: StoreConfig,
        uow_factory: Callable[[], Any],
        blob_store: Any,
        corpus_service: Any,
        *,
        adapter_factory: Callable[[], Any] = FirecrawlDirectScrapeAdapter,
        preflight: Callable[..., AuthoritativeAcquisitionContext] = (
            require_authoritative_acquisition
        ),
        authority_check: Callable[[Callable[[], Any]], None] = (
            require_direct_scrape_persistence
        ),
        queue: Any = None,
    ) -> None:
        self.config = config
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.corpus_service = corpus_service
        self.adapter_factory = adapter_factory
        self.preflight = preflight
        self.authority_check = authority_check
        self.queue = queue

    def execute(
        self,
        run_id: UUID | str,
        requests: Sequence[DirectScrapeRequest],
        *,
        idempotency_key: str | None = None,
        external_invocation_id: str | None = None,
        parent_invocation_id: UUID | str | None = None,
        retry_parent_attempt_ids: Mapping[int, UUID | str] | None = None,
    ) -> DirectScrapeBatchResult:
        if not requests:
            raise ValueError("at least one scrape request is required")
        run_uuid = UUID(str(run_id))
        context = self.preflight(run_id=run_uuid, config=self.config)
        self.authority_check(self.uow_factory)

        normalized = tuple(self._normalize_request(item) for item in requests)
        parent_uuid = (
            UUID(str(parent_invocation_id))
            if parent_invocation_id is not None
            else None
        )
        retry_parents = {
            int(index): UUID(str(attempt_id))
            for index, attempt_id in (retry_parent_attempt_ids or {}).items()
        }
        if any(index < 0 or index >= len(normalized) for index in retry_parents):
            raise ValueError("retry parent index is outside the request batch")
        batch_key = idempotency_key or self._default_idempotency_key(
            run_uuid, normalized
        )
        if not batch_key.strip():
            raise ValueError("idempotency_key must be non-empty")

        candidates = self._resolve_existing_candidates(run_uuid, normalized)
        invocation_id, saved_items, terminal_status = self._begin_or_resume(
            context,
            normalized,
            candidates,
            batch_key,
            external_invocation_id,
            parent_uuid,
        )
        if terminal_status is not None:
            items = tuple(
                DirectScrapeItemResult.from_mapping(item)
                for item in sorted(saved_items.values(), key=lambda item: item["index"])
            )
            return DirectScrapeBatchResult(
                run_id=run_uuid,
                invocation_id=invocation_id,
                idempotency_key=batch_key,
                status=terminal_status,
                items=items,
                replayed=True,
            )

        adapter = self.adapter_factory()
        resolved = self._load_resolved_targets(
            run_uuid,
            invocation_id,
            normalized,
            candidates,
            batch_key,
            retry_parents,
        )
        results: dict[str, DirectScrapeItemResult] = {
            key: DirectScrapeItemResult.from_mapping(value)
            for key, value in saved_items.items()
        }

        for target in resolved:
            if target.item_key in results:
                continue
            with self._claim_item(context, invocation_id, target.item_key) as existing:
                if existing is not None:
                    results[target.item_key] = DirectScrapeItemResult.from_mapping(
                        existing
                    )
                    continue
                transport = adapter.scrape(
                    target.canonical_url,
                    format=target.request.effective_format,
                    summary=target.request.effective_summary,
                    schema=target.request.schema,
                    options=target.request.options,
                )
                if transport.succeeded:
                    result = self._persist_success(
                        context, invocation_id, target, transport
                    )
                else:
                    result = self._persist_failure(
                        context, invocation_id, target, transport
                    )
                results[target.item_key] = result

        ordered = tuple(sorted(results.values(), key=lambda item: item.index))
        status = self._batch_status(ordered)
        self._finalize_invocation(context, invocation_id, batch_key, status, ordered)
        return DirectScrapeBatchResult(
            run_id=run_uuid,
            invocation_id=invocation_id,
            idempotency_key=batch_key,
            status=status,
            items=ordered,
        )

    def retry_failed(
        self,
        run_id: UUID | str,
        requests: Sequence[DirectScrapeRequest],
        *,
        prior_invocation_id: UUID | str,
        idempotency_key: str,
    ) -> DirectScrapeBatchResult:
        """Retry only failed items with explicit invocation and attempt lineage."""
        if not idempotency_key.strip():
            raise ValueError("retry idempotency_key must be non-empty")
        run_uuid = UUID(str(run_id))
        prior_uuid = UUID(str(prior_invocation_id))
        self.preflight(run_id=run_uuid, config=self.config)
        self.authority_check(self.uow_factory)
        normalized = tuple(self._normalize_request(item) for item in requests)
        expected_input = self._invocation_input(normalized)
        with self.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT status,idempotency_key,input,output
                    FROM research_invocations
                    WHERE id=%s AND run_id=%s AND operation='direct_scrape'""",
                (prior_uuid, run_uuid),
            )
            row = cur.fetchone()
        if row is None:
            raise DirectScrapePersistenceError(
                f"prior direct scrape invocation not found: {prior_uuid}"
            )
        status, prior_key, stored_input, output = row
        if status not in {"partial", "failed"}:
            raise DirectScrapePersistenceError(
                "only partial or failed direct scrape invocations can be retried"
            )
        if prior_key == idempotency_key:
            raise ValueError("retry requires a new idempotency_key")
        if stored_input != expected_input:
            raise DirectScrapePersistenceError(
                "retry requests do not match the prior invocation input"
            )

        failed = [
            item
            for item in sorted(
                self._items_by_key(output).values(),
                key=lambda value: value["index"],
            )
            if item.get("status") == "failed"
        ]
        if not failed:
            raise DirectScrapePersistenceError(
                "prior invocation has no failed items to retry"
            )

        retry_requests: list[DirectScrapeRequest] = []
        retry_parents: dict[int, UUID] = {}
        for retry_index, item in enumerate(failed):
            original_index = int(item["index"])
            attempt_id = item.get("extraction_attempt_id")
            if attempt_id is None:
                raise DirectScrapePersistenceError(
                    "failed item has no extraction-attempt identity"
                )
            retry_requests.append(normalized[original_index])
            retry_parents[retry_index] = UUID(str(attempt_id))

        return self.execute(
            run_uuid,
            retry_requests,
            idempotency_key=idempotency_key,
            parent_invocation_id=prior_uuid,
            retry_parent_attempt_ids=retry_parents,
        )

    @contextmanager
    def _claim_item(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        item_key: str,
    ) -> Iterator[dict[str, Any] | None]:
        """Serialize one provider execution with a session advisory lock."""
        lock_key = self._advisory_lock_key(invocation_id, item_key)
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            uow.connection.commit()
            try:
                self._revalidate_run(uow, context)
                with uow.connection.cursor() as cur:
                    cur.execute(
                        """SELECT status,output FROM research_invocations
                        WHERE id=%s AND run_id=%s""",
                        (invocation_id, self._require_context_run(context)),
                    )
                    row = cur.fetchone()
                uow.connection.commit()
                if row is None:
                    raise DirectScrapePersistenceError(
                        "direct scrape invocation disappeared before item claim"
                    )
                status, output = row
                existing = self._items_by_key(output).get(item_key)
                if existing is None and status not in {"pending", "running"}:
                    raise DirectScrapePersistenceError(
                        f"direct scrape invocation became terminal before item claim: {status}"
                    )
                yield existing
            finally:
                with uow.connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                uow.connection.commit()

    @staticmethod
    def _advisory_lock_key(invocation_id: UUID, item_key: str) -> int:
        digest = hashlib.sha256(f"{invocation_id}:{item_key}".encode()).digest()
        value = int.from_bytes(digest[:8], "big", signed=False)
        return value - (1 << 64) if value >= (1 << 63) else value

    @staticmethod
    def _normalize_request(request: DirectScrapeRequest) -> DirectScrapeRequest:
        schema = dict(request.schema) if request.schema is not None else None
        options = dict(request.options)
        candidate_id = (
            UUID(str(request.candidate_id))
            if request.candidate_id is not None
            else None
        )
        return replace(
            request,
            url=request.url.strip() if request.url is not None else None,
            candidate_id=candidate_id,
            schema=schema,
            options=options,
        )

    @staticmethod
    def _default_idempotency_key(
        run_id: UUID, requests: Sequence[DirectScrapeRequest]
    ) -> str:
        payload = [
            {
                "url": item.url,
                "candidate_id": str(item.candidate_id) if item.candidate_id else None,
                "format": item.effective_format,
                "summary": item.effective_summary,
                "schema": item.schema,
                "mime_type": item.effective_mime_type,
                "options": item.options,
            }
            for item in requests
        ]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"direct-scrape:{run_id}:{digest}"

    @staticmethod
    def _item_key(
        batch_key: str, index: int, request: DirectScrapeRequest, canonical_url: str
    ) -> str:
        payload = {
            "batch": batch_key,
            "index": index,
            "candidate_id": str(request.candidate_id) if request.candidate_id else None,
            "canonical_url": canonical_url,
            "format": request.effective_format,
            "summary": request.summary,
            "schema": request.schema,
            "mime_type": request.effective_mime_type,
            "options": request.options,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _resolve_existing_candidates(
        self, run_id: UUID, requests: Sequence[DirectScrapeRequest]
    ) -> dict[int, dict[str, Any]]:
        resolved: dict[int, dict[str, Any]] = {}
        with self.uow_factory() as uow:
            for index, request in enumerate(requests):
                if request.candidate_id is not None:
                    candidate = uow.candidates.get_candidate(
                        request.candidate_id, run_id=run_id
                    )
                    resolved[index] = candidate
                    continue
                canonical_url, _original_url = canonicalize_candidate_url(
                    request.url or ""
                )
                canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                with uow.connection.cursor() as cur:
                    cur.execute(
                        """SELECT id,canonical_url,original_url,title
                        FROM search_candidates
                        WHERE run_id=%s AND canonical_url_sha256=%s""",
                        (run_id, canonical_sha),
                    )
                    row = cur.fetchone()
                if row is not None:
                    resolved[index] = {
                        "id": row[0],
                        "canonical_url": row[1],
                        "original_url": row[2],
                        "title": row[3],
                    }
        return resolved

    def _begin_or_resume(
        self,
        context: AuthoritativeAcquisitionContext,
        requests: Sequence[DirectScrapeRequest],
        candidates: Mapping[int, Mapping[str, Any]],
        idempotency_key: str,
        external_invocation_id: str | None,
        parent_invocation_id: UUID | None,
    ) -> tuple[UUID, dict[str, dict[str, Any]], str | None]:
        run_id = self._require_context_run(context)
        input_payload = self._invocation_input(requests)
        with self.uow_factory() as uow:
            lifecycle_state, lifecycle_revision = self._revalidate_run(uow, context)
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT id,status,input,output FROM research_invocations
                    WHERE run_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (run_id, idempotency_key),
                )
                row = cur.fetchone()
                if row is not None:
                    invocation_id, status, stored_input, output = row
                    if stored_input != input_payload:
                        raise DirectScrapePersistenceError(
                            "idempotency key was used for another direct scrape request"
                        )
                    items = self._items_by_key(output)
                    if status == "cancelled":
                        raise DirectScrapePersistenceError(
                            "cancelled direct scrape invocations cannot be resumed"
                        )
                    terminal = (
                        status if status in {"complete", "partial", "failed"} else None
                    )
                    return invocation_id, items, terminal

                cur.execute(
                    """INSERT INTO research_invocations(
                    run_id,parent_invocation_id,external_invocation_id,operation,
                    status,lifecycle_revision,idempotency_key,input,output,metadata,
                    started_at)
                    VALUES(%s,%s,%s,'direct_scrape','running',%s,%s,%s,%s,%s,now())
                    RETURNING id""",
                    (
                        run_id,
                        parent_invocation_id,
                        external_invocation_id,
                        lifecycle_revision,
                        idempotency_key,
                        json.dumps(input_payload),
                        json.dumps({"schema_version": "direct-scrape-v1", "items": []}),
                        json.dumps(
                            {
                                "authority": "postgresql",
                                "payload_store": "blob",
                                "lifecycle_state": lifecycle_state,
                                "lifecycle_revision": lifecycle_revision,
                                "retry_of_invocation_id": (
                                    str(parent_invocation_id)
                                    if parent_invocation_id is not None
                                    else None
                                ),
                            }
                        ),
                    ),
                )
                invocation_id = cur.fetchone()[0]

                for index, request in enumerate(requests):
                    if request.url is None:
                        continue
                    canonical_url, original_url = canonicalize_candidate_url(
                        request.url
                    )
                    canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                    domain = urlsplit(canonical_url).hostname or "unknown"
                    cur.execute(
                        """INSERT INTO search_candidates(
                        run_id,canonical_url,canonical_url_sha256,original_url,
                        domain,backend,backend_metadata)
                        VALUES(%s,%s,%s,%s,%s,'direct_scrape',%s)
                        ON CONFLICT(run_id,canonical_url_sha256) DO UPDATE
                          SET last_seen_at=now(),
                              backend_metadata=(
                                search_candidates.backend_metadata
                                || excluded.backend_metadata
                              )
                        RETURNING id,canonical_url,original_url,title""",
                        (
                            run_id,
                            canonical_url,
                            canonical_sha,
                            original_url,
                            domain,
                            json.dumps({"direct_input_index": index}),
                        ),
                    )
                    candidate_id, stored_url, stored_original, title = cur.fetchone()
                    candidates[index] = {
                        "id": candidate_id,
                        "canonical_url": stored_url,
                        "original_url": stored_original,
                        "title": title,
                    }

            uow.runs.append_event(
                run_id,
                "direct_scrape_started",
                "system",
                f"{idempotency_key}:started",
                invocation_id=invocation_id,
                payload={
                    "item_count": len(requests),
                    "lifecycle_state": lifecycle_state,
                    "lifecycle_revision": lifecycle_revision,
                },
            )
            return invocation_id, {}, None

    @staticmethod
    def _invocation_input(
        requests: Sequence[DirectScrapeRequest],
    ) -> dict[str, Any]:
        return {
            "schema_version": "direct-scrape-v1",
            "requests": [
                {
                    "url": item.url,
                    "candidate_id": str(item.candidate_id)
                    if item.candidate_id is not None
                    else None,
                    "format": item.effective_format,
                    "summary": item.effective_summary,
                    "schema": item.schema,
                    "mime_type": item.effective_mime_type,
                    "options": item.options,
                }
                for item in requests
            ],
        }

    def _load_resolved_targets(
        self,
        run_id: UUID,
        invocation_id: UUID,
        requests: Sequence[DirectScrapeRequest],
        candidates: Mapping[int, Mapping[str, Any]],
        batch_key: str,
        retry_parent_attempt_ids: Mapping[int, UUID],
    ) -> tuple[_ResolvedTarget, ...]:
        resolved: list[_ResolvedTarget] = []
        for index, request in enumerate(requests):
            candidate = candidates[index]
            canonical_url = str(candidate["canonical_url"])
            requested_url = request.url or str(
                candidate.get("original_url") or canonical_url
            )
            resolved.append(
                _ResolvedTarget(
                    index=index,
                    item_key=self._item_key(batch_key, index, request, canonical_url),
                    request=request,
                    candidate_id=UUID(str(candidate["id"])),
                    requested_url=requested_url,
                    canonical_url=canonical_url,
                    title=candidate.get("title"),
                    retry_parent_id=retry_parent_attempt_ids.get(index),
                )
            )
        return tuple(resolved)

    def _persist_success(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
    ) -> DirectScrapeItemResult:
        run_id = self._require_context_run(context)
        raw_ref = self.blob_store.put(
            io.BytesIO(transport.raw_payload), target.request.effective_mime_type
        )
        request = IngestRequest(
            requested_url=target.requested_url,
            final_url=transport.final_url or target.canonical_url,
            content=transport.raw_payload,
            normalized_content=transport.raw_payload,
            mime_type=target.request.effective_mime_type,
            title=transport.title or target.title,
            retrieved_at=transport.responded_at,
            http_status=transport.http_status,
            firecrawl_version=str(
                transport.metadata.get("firecrawl_cli_version") or "unknown"
            ),
            crawl_options={
                "format": target.request.effective_format,
                "summary": target.request.effective_summary,
                "schema": target.request.schema,
                "options": target.request.options,
            },
            metadata={
                "direct_scrape": {
                    "candidate_id": str(target.candidate_id),
                    "invocation_id": str(invocation_id),
                    "provider_request_id": transport.provider_request_id,
                    "transport": self._bounded_mapping(transport.metadata),
                }
            },
        )
        try:
            prepared = self.corpus_service.prepare_ingest(request)
        except Exception as exc:  # noqa: BLE001
            failed = replace(
                transport,
                returncode=1,
                stderr=f"parser failure: {exc}".encode(),
                metadata={**dict(transport.metadata), "failure_class": "parser"},
            )
            return self._persist_failure(
                context, invocation_id, target, failed, raw_ref=raw_ref
            )

        try:
            with self.uow_factory() as uow:
                self._revalidate_run(uow, context)
                existing = self._existing_item(uow, invocation_id, target.item_key)
                if existing is not None:
                    return DirectScrapeItemResult.from_mapping(existing)
                attempt_number = self._next_attempt_number(uow, target.candidate_id)
                attempt_id = uow.extraction_attempts.create_attempt(
                    candidate_id=target.candidate_id,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    attempt_number=attempt_number,
                    method=self._method_for(target.request),
                    method_version="firecrawl-cli-direct-v1",
                    requested_format=target.request.effective_format,
                    start_time=transport.requested_at,
                    end_time=transport.responded_at,
                    exit_status="succeeded",
                    http_status=transport.http_status,
                    backend_status="complete",
                    raw_blob=raw_ref,
                    normalized_blob=prepared.blob,
                    parser_used=(
                        f"{prepared.parser_name}@"
                        f"{prepared.parser_implementation_version}"
                    ),
                    quality_metrics=None,
                    failure_class="none",
                    retry_parent_id=target.retry_parent_id,
                    disposition="acceptable",
                    error_message=None,
                    selection_reason="authoritative direct scrape",
                )
                prepared_request = replace(
                    prepared.request, extraction_attempt_id=attempt_id
                )
                prepared = replace(prepared, request=prepared_request)
                ingest = uow.persist_ingest(*prepared.persist_args())
                parser_identity = (
                    f"{prepared.parser_name}@"
                    f"{prepared.parser_implementation_version}:"
                    f"{prepared.parser_version}"
                )
                configuration_sha = _configuration_sha256(
                    parser_identity,
                    prepared.normalization_version,
                    prepared.chunker_name,
                    prepared.chunker_version,
                    self.config.tokenizer_name,
                )
                derivation = uow.derivations.find_by_configuration(
                    ingest.document_id, configuration_sha
                )
                if derivation is None:
                    derivation_model = uow.derivations.create(
                        document_id=ingest.document_id,
                        snapshot_id=ingest.snapshot_id,
                        parser_version=parser_identity,
                        normalization_version=prepared.normalization_version,
                        chunker_name=prepared.chunker_name,
                        chunker_version=prepared.chunker_version,
                        tokenizer_name=self.config.tokenizer_name,
                        chunk_count=len(ingest.chunk_ids),
                        block_count=len(prepared.blocks),
                        configuration_sha256=configuration_sha,
                        status="active",
                    )
                    derivation_id = derivation_model.id
                else:
                    derivation_id = UUID(str(derivation["id"]))
                uow.extraction_attempts.select_final_attempt(
                    target.candidate_id,
                    attempt_id,
                    "authoritative direct scrape",
                )
                self._link_run_asset(
                    uow, run_id, ingest.snapshot_id, invocation_id, target
                )
                result = DirectScrapeItemResult(
                    index=target.index,
                    item_key=target.item_key,
                    status="succeeded",
                    requested_url=target.requested_url,
                    canonical_url=target.canonical_url,
                    candidate_id=target.candidate_id,
                    invocation_id=invocation_id,
                    format=target.request.effective_format,
                    mime_type=target.request.effective_mime_type,
                    extraction_attempt_id=attempt_id,
                    source_id=ingest.source_id,
                    snapshot_id=ingest.snapshot_id,
                    document_id=ingest.document_id,
                    derivation_id=derivation_id,
                    chunk_ids=ingest.chunk_ids,
                    content_sha256=ingest.content_sha256,
                    raw_blob_sha256=raw_ref.sha256,
                    reused_snapshot=ingest.reused_snapshot,
                    reused_document=ingest.reused_document,
                    reused_chunks=ingest.reused_chunks,
                )
                self._record_transport_event(
                    uow,
                    run_id,
                    invocation_id,
                    attempt_id,
                    target,
                    transport,
                    "succeeded",
                )
                self._record_item(uow, run_id, invocation_id, result)
            self._notify_jobs(result.chunk_ids)
            return result
        except IndexingPersistenceError as exc:
            raise DirectScrapePersistenceError(
                f"authoritative direct scrape indexing persistence failed: {exc}",
                stage="indexing",
            ) from exc
        except Exception as exc:
            raise DirectScrapePersistenceError(
                f"authoritative direct scrape ingestion persistence failed: {exc}",
                stage="ingestion",
            ) from exc

    def _persist_failure(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
        *,
        raw_ref: Any = None,
    ) -> DirectScrapeItemResult:
        run_id = self._require_context_run(context)
        if raw_ref is None and transport.raw_payload:
            raw_ref = self.blob_store.put(
                io.BytesIO(transport.raw_payload), target.request.effective_mime_type
            )
        diagnostic = self._diagnostic(transport.stderr)
        failure_class = str(transport.metadata.get("failure_class") or "internal")
        if failure_class not in {
            "timeout",
            "network",
            "http_error",
            "parser",
            "schema_validation",
            "empty_content",
            "anti_bot",
            "unsupported_format",
            "blocked",
            "content_too_small",
            "content_too_large",
            "malformed",
            "internal",
        }:
            failure_class = "internal"
        if transport.returncode == 0 and not transport.raw_payload:
            failure_class = "empty_content"
            diagnostic = diagnostic or "provider returned an empty payload"

        with self.uow_factory() as uow:
            self._revalidate_run(uow, context)
            existing = self._existing_item(uow, invocation_id, target.item_key)
            if existing is not None:
                return DirectScrapeItemResult.from_mapping(existing)
            attempt_id = uow.extraction_attempts.create_attempt(
                candidate_id=target.candidate_id,
                run_id=run_id,
                invocation_id=invocation_id,
                attempt_number=self._next_attempt_number(uow, target.candidate_id),
                method=self._method_for(target.request),
                method_version="firecrawl-cli-direct-v1",
                requested_format=target.request.effective_format,
                start_time=transport.requested_at,
                end_time=transport.responded_at,
                exit_status="failed",
                http_status=transport.http_status,
                backend_status=f"exit:{transport.returncode}",
                raw_blob=raw_ref,
                normalized_blob=None,
                parser_used=None,
                quality_metrics=None,
                failure_class=failure_class,
                retry_parent_id=target.retry_parent_id,
                disposition="unassessed",
                error_message=diagnostic,
                selection_reason=None,
            )
            result = DirectScrapeItemResult(
                index=target.index,
                item_key=target.item_key,
                status="failed",
                requested_url=target.requested_url,
                canonical_url=target.canonical_url,
                candidate_id=target.candidate_id,
                invocation_id=invocation_id,
                format=target.request.effective_format,
                mime_type=target.request.effective_mime_type,
                extraction_attempt_id=attempt_id,
                raw_blob_sha256=raw_ref.sha256 if raw_ref is not None else None,
                error=diagnostic or "Firecrawl scrape failed",
                diagnostic=diagnostic,
                failure_class=failure_class,
            )
            self._record_transport_event(
                uow,
                run_id,
                invocation_id,
                attempt_id,
                target,
                transport,
                "failed",
            )
            self._record_item(uow, run_id, invocation_id, result)
            return result

    def _finalize_invocation(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        idempotency_key: str,
        status: str,
        items: Sequence[DirectScrapeItemResult],
    ) -> None:
        run_id = self._require_context_run(context)
        output = {
            "schema_version": "direct-scrape-v1",
            "items": [item.to_dict() for item in items],
        }
        with self.uow_factory() as uow:
            self._revalidate_run(uow, context)
            with uow.connection.cursor() as cur:
                cur.execute(
                    """UPDATE research_invocations
                    SET status=%s,output=%s,error=%s,completed_at=now()
                    WHERE id=%s AND run_id=%s AND status IN ('running','pending')
                    RETURNING id""",
                    (
                        status,
                        json.dumps(output),
                        (
                            "one or more direct scrapes failed"
                            if status != "complete"
                            else None
                        ),
                        invocation_id,
                        run_id,
                    ),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        (
                            "SELECT status,input FROM research_invocations "
                            "WHERE id=%s AND run_id=%s"
                        ),
                        (invocation_id, run_id),
                    )
                    row = cur.fetchone()
                    if row is None or row[0] != status:
                        raise DirectScrapePersistenceError(
                            "direct scrape invocation could not be finalized"
                        )
            uow.runs.append_event(
                run_id,
                "direct_scrape_finished",
                "system",
                f"{idempotency_key}:finished",
                invocation_id=invocation_id,
                payload={
                    "status": status,
                    "succeeded": sum(item.status == "succeeded" for item in items),
                    "failed": sum(item.status == "failed" for item in items),
                },
            )

    def _record_transport_event(
        self,
        uow: Any,
        run_id: UUID,
        invocation_id: UUID,
        attempt_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
        status: str,
    ) -> None:
        duration_ms = max(
            0,
            int(
                (transport.responded_at - transport.requested_at).total_seconds() * 1000
            ),
        )
        uow.runs.append_event(
            run_id,
            "direct_scrape_transport_recorded",
            "system",
            f"direct-scrape-transport:{attempt_id}",
            invocation_id=invocation_id,
            payload={
                "extraction_attempt_id": str(attempt_id),
                "candidate_id": str(target.candidate_id),
                "status": status,
                "format": target.request.effective_format,
                "mime_type": target.request.effective_mime_type,
                "requested_at": transport.requested_at.isoformat(),
                "responded_at": transport.responded_at.isoformat(),
                "duration_ms": duration_ms,
                "returncode": transport.returncode,
                "http_status": transport.http_status,
                "final_url": transport.final_url,
                "provider_request_id": transport.provider_request_id,
                "diagnostic": self._diagnostic(transport.stderr),
                "transport": self._bounded_mapping(transport.metadata),
            },
        )

    def _record_item(
        self,
        uow: Any,
        run_id: UUID,
        invocation_id: UUID,
        result: DirectScrapeItemResult,
    ) -> None:
        with uow.connection.cursor() as cur:
            cur.execute(
                (
                    "SELECT output FROM research_invocations "
                    "WHERE id=%s AND run_id=%s FOR UPDATE"
                ),
                (invocation_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError(
                    "direct scrape invocation disappeared"
                )
            output = row[0] or {"schema_version": "direct-scrape-v1", "items": []}
            items = self._items_by_key(output)
            items.setdefault(result.item_key, result.to_dict())
            ordered = sorted(items.values(), key=lambda item: item["index"])
            cur.execute(
                "UPDATE research_invocations SET output=%s WHERE id=%s AND run_id=%s",
                (
                    json.dumps(
                        {"schema_version": "direct-scrape-v1", "items": ordered}
                    ),
                    invocation_id,
                    run_id,
                ),
            )
        uow.runs.append_event(
            run_id,
            "direct_scrape_item_finished",
            "system",
            f"direct-scrape:{invocation_id}:{result.item_key}",
            invocation_id=invocation_id,
            payload={
                "index": result.index,
                "status": result.status,
                "candidate_id": str(result.candidate_id),
                "snapshot_id": str(result.snapshot_id) if result.snapshot_id else None,
                "document_id": str(result.document_id) if result.document_id else None,
                "extraction_attempt_id": str(result.extraction_attempt_id)
                if result.extraction_attempt_id
                else None,
            },
        )

    @staticmethod
    def _existing_item(
        uow: Any, invocation_id: UUID, item_key: str
    ) -> dict[str, Any] | None:
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT output FROM research_invocations WHERE id=%s FOR UPDATE",
                (invocation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError("direct scrape invocation not found")
        return DirectScrapeService._items_by_key(row[0]).get(item_key)

    @staticmethod
    def _items_by_key(output: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not output:
            return {}
        items = output.get("items", [])
        if not isinstance(items, list):
            raise DirectScrapePersistenceError(
                "invalid direct scrape invocation output"
            )
        return {
            str(item["item_key"]): dict(item)
            for item in items
            if isinstance(item, Mapping) and item.get("item_key")
        }

    @staticmethod
    def _next_attempt_number(uow: Any, candidate_id: UUID) -> int:
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM search_candidates WHERE id=%s FOR UPDATE",
                (candidate_id,),
            )
            if cur.fetchone() is None:
                raise DirectScrapePersistenceError(
                    f"direct scrape candidate disappeared: {candidate_id}"
                )
            cur.execute(
                (
                    "SELECT COALESCE(MAX(attempt_number),0)+1 "
                    "FROM extraction_attempts WHERE candidate_id=%s"
                ),
                (candidate_id,),
            )
            return int(cur.fetchone()[0])

    @staticmethod
    def _link_run_asset(
        uow: Any,
        run_id: UUID,
        snapshot_id: UUID,
        invocation_id: UUID,
        target: _ResolvedTarget,
    ) -> None:
        with uow.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                VALUES(%s,%s,'acquired',%s)
                ON CONFLICT(run_id,snapshot_id,role) DO UPDATE
                  SET metadata=research_run_assets.metadata || excluded.metadata""",
                (
                    run_id,
                    snapshot_id,
                    json.dumps(
                        {
                            "invocation_id": str(invocation_id),
                            "candidate_id": str(target.candidate_id),
                            "item_key": target.item_key,
                        }
                    ),
                ),
            )

    @staticmethod
    def _method_for(request: DirectScrapeRequest) -> str:
        if request.schema is not None:
            return "structured_extraction"
        if request.effective_format == "markdown" and not request.summary:
            return "firecrawl_main_content"
        return "firecrawl_full_page"

    @staticmethod
    def _batch_status(items: Sequence[DirectScrapeItemResult]) -> str:
        succeeded = sum(item.status == "succeeded" for item in items)
        if succeeded == len(items):
            return "complete"
        if succeeded:
            return "partial"
        return "failed"

    @staticmethod
    def _require_context_run(context: AuthoritativeAcquisitionContext) -> UUID:
        if context.run_id is None or context.lifecycle_revision is None:
            raise DirectScrapePersistenceError(
                "direct scrape requires a run-bound authoritative context"
            )
        return context.run_id

    @staticmethod
    def _revalidate_run(
        uow: Any,
        context: AuthoritativeAcquisitionContext,
    ) -> tuple[str, int]:
        run_id = DirectScrapeService._require_context_run(context)
        with uow.connection.cursor() as cur:
            cur.execute(
                (
                    "SELECT state,lifecycle_revision FROM research_runs "
                    "WHERE id=%s FOR UPDATE"
                ),
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError(
                    f"research run disappeared: {run_id}"
                )
            state, revision = str(row[0]), int(row[1])
            if state not in ACQUISITION_ENTRY_STATES:
                raise DirectScrapePersistenceError(
                    f"research run is no longer acquisition-eligible: {state}"
                )
            if revision != context.lifecycle_revision:
                raise DirectScrapePersistenceError(
                    "research run lifecycle revision changed before persistence: "
                    f"expected {context.lifecycle_revision}, current {revision}"
                )
            return state, revision

    def _notify_jobs(self, chunk_ids: Sequence[UUID]) -> None:
        if self.queue is None:
            return
        for chunk_id in chunk_ids:
            try:
                self.queue.notify(chunk_id)
            except Exception:  # noqa: BLE001, S110
                pass

    @staticmethod
    def _diagnostic(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")[:_MAX_DIAGNOSTIC_CHARS].strip()

    @staticmethod
    def _bounded_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, default=str, sort_keys=True)
        if len(encoded) <= _MAX_DIAGNOSTIC_CHARS:
            return dict(value)
        return {"truncated": encoded[:_MAX_DIAGNOSTIC_CHARS]}


def build_direct_scrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], Any] = FirecrawlDirectScrapeAdapter,
) -> DirectScrapeService:
    """Build the authoritative direct scrape service from store configuration."""
    from functools import partial

    from .blob import ContentAddressedBlobStore
    from .parsing import get_registry
    from .postgres import PostgresUnitOfWork
    from .service import CorpusService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    uow_factory = partial(
        PostgresUnitOfWork,
        resolved.database_url,
        resolved.physical_collection,
        resolved.embedding_model,
        resolved.embedding_revision,
        resolved.embedding_dimension,
        resolved.parser_version,
        resolved.normalization_version,
        resolved.chunker_version,
    )
    blob_store = ContentAddressedBlobStore(resolved.blob_root)
    corpus_service = CorpusService(
        resolved,
        uow_factory,
        blob_store,
        parser_registry=get_registry(),
    )
    return DirectScrapeService(
        resolved,
        uow_factory,
        blob_store,
        corpus_service,
        adapter_factory=adapter_factory,
    )
