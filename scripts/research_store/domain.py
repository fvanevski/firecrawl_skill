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
    # Legacy placeholder default — typed parsers override this with their
    # own version string (e.g. "markdown-v1", "html-normalized-v1").
    parser_version: str = "canonical-v1"


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    content_sha256: str
    first_block_ordinal: int
    last_block_ordinal: int
    token_count: int
    heading_path: tuple[str, ...] = ()
    # Hierarchical chunking fields (P5-06).  Legacy structural chunks leave
    # these at their default (None).  Hierarchical chunks set them explicitly
    # during the HierarchicalChunk → Chunk conversion in CorpusService.
    tokenizer_name: str | None = None
    parent_block_ordinal: int | None = None


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
    extraction_attempt_id: UUID | None = None


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


@dataclass(frozen=True)
class SearchCandidate:
    id: UUID
    run_id: UUID
    canonical_url: str
    canonical_url_sha256: str
    original_url: str
    domain: str
    backend: str
    title: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None
    date_signals: dict[str, Any] = field(default_factory=dict)
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    recurrence_count: int = 1
    duplicate_group_id: UUID | None = None
    first_seen_at: datetime = field(default_factory=utcnow)
    last_seen_at: datetime = field(default_factory=utcnow)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class CandidateOccurrence:
    id: UUID
    candidate_id: UUID
    run_id: UUID
    search_response_id: UUID
    rank: int
    query_text: str
    original_url: str
    plan_id: UUID | None = None
    plan_query_id: UUID | None = None
    title: str | None = None
    snippet: str | None = None
    raw_item: dict[str, Any] = field(default_factory=dict)
    discovered_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class ClaimRecord:
    """A persisted claim record stored in ``research_claims``.

    This is the authoritative PostgreSQL representation of a research claim.
    Domain-level ``claim_id`` (from ``EvidenceClaim``) is preserved as a
    separate column so that domain UUIDs are queryable alongside the
    surrogate ``id``.
    """

    id: UUID
    run_id: UUID
    claim_id: UUID
    statement: str
    semantic_status: str
    uncertainty: str | None
    evidence_packet_revision: int
    created_at: datetime

    _VALID_STATUSES = frozenset(
        {
            "supported",
            "contradicted",
            "qualified",
            "unsupported",
            "uncertain",
            "unassessed",
        }
    )

    def __post_init__(self):
        if not self.statement.strip():
            raise ValueError("claim statement must be non-empty")
        if self.semantic_status not in self._VALID_STATUSES:
            raise ValueError(f"invalid semantic_status: {self.semantic_status}")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ClaimRecord":
        def _uuid(v):
            return UUID(v) if not isinstance(v, UUID) else v

        return cls(
            id=_uuid(value["id"]),
            run_id=_uuid(value["run_id"]),
            claim_id=_uuid(value["claim_id"]),
            statement=value["statement"],
            semantic_status=value["semantic_status"],
            uncertainty=value.get("uncertainty"),
            evidence_packet_revision=value.get("evidence_packet_revision", 1),
            created_at=value["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "claim_id": str(self.claim_id),
            "statement": self.statement,
            "semantic_status": self.semantic_status,
            "uncertainty": self.uncertainty,
            "evidence_packet_revision": self.evidence_packet_revision,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }


@dataclass(frozen=True)
class ClaimEvidenceLink:
    """A persisted claim-to-passage evidence link.

    Stored in ``claim_evidence_links``.  Append-only — no UPDATE/DELETE.
    """

    id: UUID
    run_id: UUID
    claim_id: UUID
    passage_id: UUID
    snapshot_id: UUID
    source_url: str
    relationship: str
    confidence: float
    created_at: datetime

    _VALID_RELATIONSHIPS = frozenset(
        {"supports", "contradicts", "qualifies", "context"}
    )

    def __post_init__(self):
        if self.relationship not in self._VALID_RELATIONSHIPS:
            raise ValueError(f"invalid relationship: {self.relationship}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ClaimEvidenceLink":
        def _uuid(v):
            return UUID(v) if not isinstance(v, UUID) else v

        return cls(
            id=_uuid(value["id"]),
            run_id=_uuid(value["run_id"]),
            claim_id=_uuid(value["claim_id"]),
            passage_id=_uuid(value["passage_id"]),
            snapshot_id=_uuid(value["snapshot_id"]),
            source_url=value.get("source_url", ""),
            relationship=value.get("relationship", "supports"),
            confidence=value.get("confidence", 1.0),
            created_at=value["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "claim_id": str(self.claim_id),
            "passage_id": str(self.passage_id),
            "snapshot_id": str(self.snapshot_id),
            "source_url": self.source_url,
            "relationship": self.relationship,
            "confidence": self.confidence,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }


def new_id() -> UUID:
    return uuid4()


@dataclass(frozen=True)
class SearchAdapterResult:
    raw_payload: bytes
    http_status: int | None = None
    provider_request_id: str | None = None
    transport_error: str | None = None
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    requested_at: datetime = field(default_factory=utcnow)
    responded_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class CandidateCard:
    id: UUID
    run_id: UUID
    canonical_url: str
    original_url: str
    domain: str
    title: str | None = None
    snippet: str | None = None
    published_at: str | None = None
    recurrence_count: int = 1
    duplicate_group_id: UUID | None = None
    date_signals: dict[str, Any] = field(default_factory=dict)
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    occurrences: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "canonical_url": self.canonical_url,
            "original_url": self.original_url,
            "domain": self.domain,
            "title": self.title,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "recurrence_count": self.recurrence_count,
            "duplicate_group_id": self.duplicate_group_id,
            "date_signals": self.date_signals,
            "backend_metadata": self.backend_metadata,
            "occurrences": self.occurrences,
        }


@dataclass(frozen=True)
class PaginatedCandidates:
    items: list[dict[str, Any]]
    total_count: int
    limit: int
    offset: int
    has_next: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "has_next": self.has_next,
        }


# ---------------------------------------------------------------------------
# Audit domain models (issue #33)
# ---------------------------------------------------------------------------

VALID_AUDIT_STATUSES = frozenset({"completed", "partial", "failed"})
VALID_AUDIT_STAGES = frozenset({"rubric", "acquisition", "evidence", "synthesis"})
VALID_AUDIT_STAGE_STATUSES = frozenset({"completed", "failed", "skipped"})
VALID_AUDIT_TARGET_TYPES = frozenset({"run", "invocation"})


@dataclass(frozen=True)
class AuditAssessment:
    """Represents a staged semantic audit assessment."""

    id: UUID
    run_id: UUID
    target_type: str  # 'run' | 'invocation'
    target_id: UUID
    target_hash: str
    evaluator_version: str
    prompt_template_version: str
    policy_version: str
    stage_set: tuple[str, ...]
    status: str  # 'completed' | 'partial' | 'failed'
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    model_fingerprint: str | None = None
    elapsed_ms: int = 0
    audit_packet_manifest: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.target_type not in VALID_AUDIT_TARGET_TYPES:
            raise ValueError(
                f"invalid target_type: {self.target_type}; "
                f"expected one of {sorted(VALID_AUDIT_TARGET_TYPES)}"
            )
        if self.status not in VALID_AUDIT_STATUSES:
            raise ValueError(
                f"invalid status: {self.status}; "
                f"expected one of {sorted(VALID_AUDIT_STATUSES)}"
            )

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> AuditAssessment:
        stage_set = row.get("stage_set")
        if isinstance(stage_set, str):
            # PostgreSQL returns text[] as a Python list
            stage_set = tuple(stage_set)
        elif isinstance(stage_set, (list, tuple)):
            stage_set = tuple(stage_set)
        else:
            stage_set = ()
        return cls(
            id=UUID(row["id"]),
            run_id=UUID(row["run_id"]),
            target_type=row["target_type"],
            target_id=UUID(row["target_id"]),
            target_hash=row["target_hash"],
            evaluator_version=row["evaluator_version"],
            prompt_template_version=row["prompt_template_version"],
            policy_version=row["policy_version"],
            stage_set=stage_set,
            status=row["status"],
            provider=row.get("provider"),
            model=row.get("model"),
            prompt_hash=row.get("prompt_hash"),
            model_fingerprint=row.get("model_fingerprint"),
            elapsed_ms=row.get("elapsed_ms", 0),
            audit_packet_manifest=row.get("audit_packet_manifest"),
            created_at=_parse_timestamptz(row.get("created_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "target_type": self.target_type,
            "target_id": str(self.target_id),
            "target_hash": self.target_hash,
            "evaluator_version": self.evaluator_version,
            "prompt_template_version": self.prompt_template_version,
            "policy_version": self.policy_version,
            "stage_set": list(self.stage_set),
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "model_fingerprint": self.model_fingerprint,
            "elapsed_ms": self.elapsed_ms,
            "audit_packet_manifest": self.audit_packet_manifest,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class AuditStageOutput:
    """Represents an individual stage output within an assessment."""

    id: UUID
    assessment_id: UUID
    stage: str  # 'rubric' | 'acquisition' | 'evidence' | 'synthesis'
    sequence_number: int
    status: str  # 'completed' | 'failed' | 'skipped'
    output: dict[str, Any] | None = None
    error: str | None = None
    error_details: dict[str, Any] | None = None
    call_count: int = 0
    used_fallback: bool = False
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.stage not in VALID_AUDIT_STAGES:
            raise ValueError(
                f"invalid stage: {self.stage}; "
                f"expected one of {sorted(VALID_AUDIT_STAGES)}"
            )
        if self.status not in VALID_AUDIT_STAGE_STATUSES:
            raise ValueError(
                f"invalid status: {self.status}; "
                f"expected one of {sorted(VALID_AUDIT_STAGE_STATUSES)}"
            )
        if self.sequence_number < 1:
            raise ValueError("sequence_number must be >= 1")

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> AuditStageOutput:
        return cls(
            id=UUID(row["id"]),
            assessment_id=UUID(row["assessment_id"]),
            stage=row["stage"],
            sequence_number=int(row["sequence_number"]),
            status=row["status"],
            output=row.get("output"),
            error=row.get("error"),
            error_details=row.get("error_details"),
            call_count=int(row.get("call_count", 0)),
            used_fallback=bool(row.get("used_fallback", False)),
            created_at=_parse_timestamptz(row.get("created_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "assessment_id": str(self.assessment_id),
            "stage": self.stage,
            "sequence_number": self.sequence_number,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "error_details": self.error_details,
            "call_count": self.call_count,
            "used_fallback": self.used_fallback,
            "created_at": self.created_at.isoformat(),
        }


def _parse_timestamptz(value: str | None) -> datetime:
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return utcnow()


# ---------------------------------------------------------------------------
# Extraction-attempt domain models (issue #40)
# ---------------------------------------------------------------------------

VALID_EXTRACTION_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})
VALID_EXTRACTION_FAILURE_CLASSES = frozenset(
    {
        "none",
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
    }
)
VALID_EXTRACTION_DISPOSITIONS = frozenset(
    {"acceptable", "poor", "ambiguous", "unassessed"}
)
VALID_EXTRACTION_METHODS = frozenset(
    {
        "firecrawl_main_content",
        "firecrawl_full_page",
        "deterministic_html",
        "deterministic_markdown",
        "deterministic_json",
        "deterministic_plain_text",
        "browser_capable",
        "alternate_adapter",
        "structured_extraction",
        "semantic_adjudication",
    }
)


@dataclass(frozen=True)
class ExtractionQualityMetrics:
    """Deterministic quality metrics for an extraction attempt.

    Stored separately from the attempt record so that quality
    evolution can be tracked without mutating the attempt itself.
    """

    byte_length: int = 0
    visible_text_length: int = 0
    paragraph_count: int = 0
    heading_count: int = 0
    list_count: int = 0
    table_count: int = 0
    link_density: float = 0.0
    boilerplate_ratio: float = 0.0
    title_present: bool = False
    language_confidence: float = 0.0
    content_type_consistent: bool = True
    anti_bot_markers: int = 0
    duplicate_content_similarity: float = 0.0
    query_term_coverage: float = 0.0
    required_structured_fields: int = 0
    parser_warnings: int = 0
    code_to_prose_ratio: float = 0.0
    extraction_method_confidence: float = 0.0
    encoding_anomaly: bool = False
    quality_version: str = "quality-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_length": self.byte_length,
            "visible_text_length": self.visible_text_length,
            "paragraph_count": self.paragraph_count,
            "heading_count": self.heading_count,
            "list_count": self.list_count,
            "table_count": self.table_count,
            "link_density": self.link_density,
            "boilerplate_ratio": self.boilerplate_ratio,
            "title_present": self.title_present,
            "language_confidence": self.language_confidence,
            "content_type_consistent": self.content_type_consistent,
            "anti_bot_markers": self.anti_bot_markers,
            "duplicate_content_similarity": self.duplicate_content_similarity,
            "query_term_coverage": self.query_term_coverage,
            "required_structured_fields": self.required_structured_fields,
            "parser_warnings": self.parser_warnings,
            "code_to_prose_ratio": self.code_to_prose_ratio,
            "extraction_method_confidence": self.extraction_method_confidence,
            "encoding_anomaly": self.encoding_anomaly,
            "quality_version": self.quality_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionQualityMetrics":
        return cls(
            byte_length=data.get("byte_length", 0),
            visible_text_length=data.get("visible_text_length", 0),
            paragraph_count=data.get("paragraph_count", 0),
            heading_count=data.get("heading_count", 0),
            list_count=data.get("list_count", 0),
            table_count=data.get("table_count", 0),
            link_density=float(data.get("link_density", 0.0)),
            boilerplate_ratio=float(data.get("boilerplate_ratio", 0.0)),
            title_present=bool(data.get("title_present", False)),
            language_confidence=float(data.get("language_confidence", 0.0)),
            content_type_consistent=bool(data.get("content_type_consistent", True)),
            anti_bot_markers=data.get("anti_bot_markers", 0),
            duplicate_content_similarity=float(
                data.get("duplicate_content_similarity", 0.0)
            ),
            query_term_coverage=float(data.get("query_term_coverage", 0.0)),
            required_structured_fields=data.get("required_structured_fields", 0),
            parser_warnings=data.get("parser_warnings", 0),
            code_to_prose_ratio=float(data.get("code_to_prose_ratio", 0.0)),
            extraction_method_confidence=float(
                data.get("extraction_method_confidence", 0.0)
            ),
            encoding_anomaly=bool(data.get("encoding_anomaly", False)),
            quality_version=data.get("quality_version", "quality-v1"),
        )


@dataclass(frozen=True)
class ExtractionAttempt:
    """Represents one extraction method invocation for a candidate.

    One row per extraction method attempt.  Multiple attempts per
    candidate are ordered by ``attempt_number``.  Retries link to
    their parent attempt via ``retry_parent_id``.
    """

    id: UUID
    candidate_id: UUID
    run_id: UUID
    invocation_id: UUID | None
    attempt_number: int
    method: str
    method_version: str
    requested_format: str | None
    start_time: datetime
    end_time: datetime | None
    exit_status: str  # 'succeeded' | 'partial' | 'failed' | 'cancelled'
    http_status: int | None
    backend_status: str | None
    raw_blob: BlobReference | None
    normalized_blob: BlobReference | None
    parser_used: str | None
    quality_metrics: ExtractionQualityMetrics | None
    failure_class: str
    retry_parent_id: UUID | None
    disposition: str  # 'acceptable' | 'poor' | 'ambiguous' | 'unassessed'
    error_message: str | None
    selection_reason: str | None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.method not in VALID_EXTRACTION_METHODS:
            raise ValueError(
                f"invalid extraction method: {self.method}; "
                f"expected one of {sorted(VALID_EXTRACTION_METHODS)}"
            )
        if self.exit_status not in VALID_EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid exit_status: {self.exit_status}; "
                f"expected one of {sorted(VALID_EXTRACTION_STATUSES)}"
            )
        if self.failure_class not in VALID_EXTRACTION_FAILURE_CLASSES:
            raise ValueError(
                f"invalid failure_class: {self.failure_class}; "
                f"expected one of {sorted(VALID_EXTRACTION_FAILURE_CLASSES)}"
            )
        if self.disposition not in VALID_EXTRACTION_DISPOSITIONS:
            raise ValueError(
                f"invalid disposition: {self.disposition}; "
                f"expected one of {sorted(VALID_EXTRACTION_DISPOSITIONS)}"
            )
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "ExtractionAttempt":
        def _uuid(v):
            return UUID(v) if not isinstance(v, UUID) and v is not None else v

        raw_blob = None
        rb = row.get("raw_blob")
        if rb and isinstance(rb, dict):
            raw_blob = BlobReference(
                sha256=rb["sha256"],
                uri=rb["uri"],
                byte_length=rb["byte_length"],
                mime_type=rb.get("mime_type"),
            )

        normalized_blob = None
        nb = row.get("normalized_blob")
        if nb and isinstance(nb, dict):
            normalized_blob = BlobReference(
                sha256=nb["sha256"],
                uri=nb["uri"],
                byte_length=nb["byte_length"],
                mime_type=nb.get("mime_type"),
            )

        quality_metrics = None
        qm = row.get("quality_metrics")
        if qm and isinstance(qm, dict):
            quality_metrics = ExtractionQualityMetrics.from_dict(qm)

        return cls(
            id=UUID(row["id"]),
            candidate_id=UUID(row["candidate_id"]),
            run_id=UUID(row["run_id"]),
            invocation_id=_uuid(row.get("invocation_id")),
            attempt_number=int(row["attempt_number"]),
            method=row["method"],
            method_version=row["method_version"],
            requested_format=row.get("requested_format"),
            start_time=_parse_timestamptz(row.get("start_time")),
            end_time=_parse_timestamptz(row.get("end_time")),
            exit_status=row["exit_status"],
            http_status=row.get("http_status"),
            backend_status=row.get("backend_status"),
            raw_blob=raw_blob,
            normalized_blob=normalized_blob,
            parser_used=row.get("parser_used"),
            quality_metrics=quality_metrics,
            failure_class=row.get("failure_class", "none"),
            retry_parent_id=_uuid(row.get("retry_parent_id")),
            disposition=row.get("disposition", "unassessed"),
            error_message=row.get("error_message"),
            selection_reason=row.get("selection_reason"),
            created_at=_parse_timestamptz(row.get("created_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "candidate_id": str(self.candidate_id),
            "run_id": str(self.run_id),
            "invocation_id": str(self.invocation_id) if self.invocation_id else None,
            "attempt_number": self.attempt_number,
            "method": self.method,
            "method_version": self.method_version,
            "requested_format": self.requested_format,
            "start_time": (
                self.start_time.isoformat()
                if hasattr(self.start_time, "isoformat")
                else str(self.start_time)
            ),
            "end_time": (
                self.end_time.isoformat()
                if self.end_time is not None and hasattr(self.end_time, "isoformat")
                else None
            ),
            "exit_status": self.exit_status,
            "http_status": self.http_status,
            "backend_status": self.backend_status,
            "raw_blob": (
                {
                    "sha256": self.raw_blob.sha256,
                    "uri": self.raw_blob.uri,
                    "byte_length": self.raw_blob.byte_length,
                    "mime_type": self.raw_blob.mime_type,
                }
                if self.raw_blob
                else None
            ),
            "normalized_blob": (
                {
                    "sha256": self.normalized_blob.sha256,
                    "uri": self.normalized_blob.uri,
                    "byte_length": self.normalized_blob.byte_length,
                    "mime_type": self.normalized_blob.mime_type,
                }
                if self.normalized_blob
                else None
            ),
            "parser_used": self.parser_used,
            "quality_metrics": (
                self.quality_metrics.to_dict() if self.quality_metrics else None
            ),
            "failure_class": self.failure_class,
            "retry_parent_id": str(self.retry_parent_id)
            if self.retry_parent_id
            else None,
            "disposition": self.disposition,
            "error_message": self.error_message,
            "selection_reason": self.selection_reason,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }


# ---------------------------------------------------------------------------
# Normalization domain models (issue #45)
# ---------------------------------------------------------------------------

VALID_NORMALIZATION_DISPOSITIONS = frozenset({"keep", "alter", "suppress", "remove"})
VALID_NORMALIZATION_RULE_IDS = frozenset(
    {
        "strip-cookie-notice",
        "strip-navigation",
        "strip-social-links",
        "strip-boilerplate-heading",
        "preserve-citation",
        "preserve-code-block",
        "preserve-meaningful-link",
        "preserve-short-heading",
        "preserve-footnote",
        "preserve-source-url",
        "doc-type-footer-digest",
        "no-change",
    }
)


@dataclass(frozen=True)
class NormalizedBlock:
    """A block after normalization, with a disposition and rule version.

    Attributes:
        id: Stable UUID for the normalized block.
        source_block_id: FK to the source ``document_blocks.id``.
        document_id: FK to ``documents(id)``.
        ordinal: Positional index within the document.
        block_type: Semantic type tag (inherited from source).
        text: Normalized text content.
        heading_path: Ancestor heading path.
        disposition: One of keep, alter, suppress, remove.
        rule_version: Version of the normalization rule applied.
        transformation_reason: Why this disposition was chosen.
        parser_version: Parser version that produced the source block.
    """

    id: UUID
    source_block_id: UUID
    document_id: UUID | None
    ordinal: int
    block_type: str
    text: str
    heading_path: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None
    disposition: str = "keep"
    rule_version: str = "normalization-v1"
    transformation_reason: str | None = None
    parser_version: str = "canonical-v1"

    def __post_init__(self) -> None:
        if self.disposition not in VALID_NORMALIZATION_DISPOSITIONS:
            raise ValueError(
                f"invalid disposition: {self.disposition}; "
                f"expected one of {sorted(VALID_NORMALIZATION_DISPOSITIONS)}"
            )

    @classmethod
    def from_source_block(
        cls,
        source_block_id: UUID,
        document_id: UUID | None,
        ordinal: int,
        block_type: str,
        text: str,
        heading_path: tuple[str, ...] = (),
        char_start: int | None = None,
        char_end: int | None = None,
        disposition: str = "keep",
        rule_version: str = "normalization-v1",
        transformation_reason: str | None = None,
        parser_version: str = "canonical-v1",
        id: UUID | None = None,
    ) -> "NormalizedBlock":
        """Create a normalized block from a source block snapshot.

        Args:
            source_block_id: UUID of the source block.
            document_id: UUID of the parent document.
            ordinal: Block ordinal.
            block_type: Block type.
            text: Block text (may be normalized).
            heading_path: Heading path tuple.
            disposition: Normalization disposition.
            rule_version: Normalization rule version.
            transformation_reason: Why this disposition was chosen.
            parser_version: Parser version from the source block.

        Returns:
            A new ``NormalizedBlock`` instance.
        """
        return cls(
            id=id or uuid4(),
            source_block_id=source_block_id,
            document_id=document_id,
            ordinal=ordinal,
            block_type=block_type,
            text=text,
            heading_path=heading_path,
            char_start=char_start,
            char_end=char_end,
            disposition=disposition,
            rule_version=rule_version,
            transformation_reason=transformation_reason,
            parser_version=parser_version,
        )


@dataclass(frozen=True)
class TransformationRecord:
    """Records a single transformation applied during normalization.

    Every time a normalization rule modifies or acts on a block, a
    ``TransformationRecord`` is created so that the change is auditable
    and reversible.

    Attributes:
        id: Stable UUID for the transformation record.
        normalized_block_id: FK to ``normalized_blocks(id)``.
        rule_id: Identifier of the rule that was applied.
        rule_version: Version of the normalization rule.
        reason: Human-readable reason for the transformation.
        before_text: Text before the transformation (may be empty).
        after_text: Text after the transformation (may be empty).
        confidence: Confidence score in [0, 1] for the rule decision.
    """

    id: UUID = field(default_factory=uuid4)
    normalized_block_id: UUID | None = None
    rule_id: str = ""
    rule_version: str = "normalization-v1"
    reason: str = ""
    before_text: str = ""
    after_text: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.rule_id not in VALID_NORMALIZATION_RULE_IDS:
            raise ValueError(
                f"invalid rule_id: {self.rule_id}; "
                f"expected one of {sorted(VALID_NORMALIZATION_RULE_IDS)}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @classmethod
    def create(
        cls,
        normalized_block_id: UUID,
        rule_id: str,
        reason: str,
        before_text: str = "",
        after_text: str = "",
        confidence: float = 1.0,
        rule_version: str = "normalization-v1",
    ) -> "TransformationRecord":
        """Create a transformation record.

        Args:
            normalized_block_id: UUID of the normalized block.
            rule_id: Rule identifier.
            reason: Human-readable reason.
            before_text: Text before transformation.
            after_text: Text after transformation.
            confidence: Confidence score in [0, 1].
            rule_version: Rule version.

        Returns:
            A new ``TransformationRecord`` instance.
        """
        return cls(
            id=uuid4(),
            normalized_block_id=normalized_block_id,
            rule_id=rule_id,
            rule_version=rule_version,
            reason=reason,
            before_text=before_text,
            after_text=after_text,
            confidence=confidence,
        )
