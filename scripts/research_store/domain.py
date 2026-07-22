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
