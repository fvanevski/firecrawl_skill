"""Typed repository read models for stable research-store identities.

Repository adapters materialize these records before values cross into the
application layer.  Public/legacy JSON projection is intentionally explicit and
separate so internal code never has to infer whether ``id`` means a candidate,
an occurrence, or another persisted object.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID


def _uuid(value: Any, *, field: str) -> UUID:
    if value is None:
        raise ValueError(f"{field} is required")
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _optional_uuid(value: Any, *, field: str) -> UUID | None:
    return None if value is None else _uuid(value, field=field)


def _canonical_candidate_id(value: Mapping[str, Any]) -> UUID:
    """Validate a canonical candidate identity at an adapter boundary.

    ``candidate_id`` is the only application-layer identity name.  A producer
    that still supplies the historical ``id`` alias may be diagnosed here, but
    it cannot override or substitute for the canonical field.
    """

    if "candidate_id" not in value or value.get("candidate_id") is None:
        raise ValueError("candidate read model requires candidate_id")
    candidate_id = _uuid(value["candidate_id"], field="candidate_id")
    legacy_id = value.get("id")
    if legacy_id is not None and _uuid(legacy_id, field="id") != candidate_id:
        raise ValueError("conflicting candidate_id and legacy id")
    return candidate_id


@dataclass(frozen=True)
class CandidateRecord:
    """Canonical read model for one persisted search candidate."""

    candidate_id: UUID
    run_id: UUID
    canonical_url: str
    canonical_url_sha256: str
    original_url: str
    title: str | None
    snippet: str | None
    domain: str
    backend: str
    published_at: datetime | None
    date_signals: dict[str, Any]
    backend_metadata: dict[str, Any]
    recurrence_count: int
    duplicate_group_id: UUID | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    independence_assessment: dict[str, Any] | None

    @classmethod
    def from_repository_row(cls, row: Sequence[Any]) -> "CandidateRecord":
        if len(row) != 18:
            raise ValueError(
                f"candidate repository row has {len(row)} fields; expected 18"
            )
        return cls(
            candidate_id=_uuid(row[0], field="candidate_id"),
            run_id=_uuid(row[1], field="run_id"),
            canonical_url=str(row[2]),
            canonical_url_sha256=str(row[3]),
            original_url=str(row[4]),
            title=None if row[5] is None else str(row[5]),
            snippet=None if row[6] is None else str(row[6]),
            domain=str(row[7]),
            backend=str(row[8]),
            published_at=row[9],
            date_signals=dict(row[10] or {}),
            backend_metadata=dict(row[11] or {}),
            recurrence_count=int(row[12]),
            duplicate_group_id=_optional_uuid(row[13], field="duplicate_group_id"),
            first_seen_at=row[14],
            last_seen_at=row[15],
            created_at=row[16],
            independence_assessment=(
                None if row[17] is None else dict(row[17])
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateRecord":
        """Strict mapping adapter used at non-SQL repository boundaries/tests."""

        candidate_id = _canonical_candidate_id(value)
        required = (
            "run_id",
            "canonical_url",
            "canonical_url_sha256",
            "original_url",
            "domain",
            "backend",
            "recurrence_count",
            "first_seen_at",
            "last_seen_at",
            "created_at",
        )
        missing = [field for field in required if value.get(field) is None]
        if missing:
            raise ValueError(f"candidate read model missing fields: {missing}")
        return cls(
            candidate_id=candidate_id,
            run_id=_uuid(value["run_id"], field="run_id"),
            canonical_url=str(value["canonical_url"]),
            canonical_url_sha256=str(value["canonical_url_sha256"]),
            original_url=str(value["original_url"]),
            title=None if value.get("title") is None else str(value["title"]),
            snippet=None if value.get("snippet") is None else str(value["snippet"]),
            domain=str(value["domain"]),
            backend=str(value["backend"]),
            published_at=value.get("published_at"),
            date_signals=dict(value.get("date_signals") or {}),
            backend_metadata=dict(value.get("backend_metadata") or {}),
            recurrence_count=int(value["recurrence_count"]),
            duplicate_group_id=_optional_uuid(
                value.get("duplicate_group_id"), field="duplicate_group_id"
            ),
            first_seen_at=value["first_seen_at"],
            last_seen_at=value["last_seen_at"],
            created_at=value["created_at"],
            independence_assessment=(
                None
                if value.get("independence_assessment") is None
                else dict(value["independence_assessment"])
            ),
        )

    @property
    def identity_urls(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for value in (self.canonical_url, self.original_url)
                if value
            )
        )


@dataclass(frozen=True)
class CandidateOccurrenceRecord:
    """Canonical occurrence read model with separate occurrence/candidate IDs."""

    occurrence_id: UUID
    candidate_id: UUID
    run_id: UUID
    search_response_id: UUID
    plan_id: UUID | None
    plan_query_id: UUID | None
    rank: int
    query_text: str
    canonical_url: str | None
    original_url: str | None
    title: str | None
    snippet: str | None
    raw_item: dict[str, Any]
    discovered_at: datetime | None = None
    temporal_assessment: dict[str, Any] | None = None
    branches: tuple[str, ...] = ()

    @classmethod
    def from_repository_row(
        cls,
        row: Sequence[Any],
        *,
        canonical_url: str | None = None,
    ) -> "CandidateOccurrenceRecord":
        if len(row) != 13:
            raise ValueError(
                f"candidate occurrence row has {len(row)} fields; expected 13"
            )
        return cls(
            occurrence_id=_uuid(row[0], field="occurrence_id"),
            candidate_id=_uuid(row[1], field="candidate_id"),
            run_id=_uuid(row[2], field="run_id"),
            search_response_id=_uuid(row[3], field="search_response_id"),
            plan_id=_optional_uuid(row[4], field="plan_id"),
            plan_query_id=_optional_uuid(row[5], field="plan_query_id"),
            rank=int(row[6]),
            query_text=str(row[7]),
            canonical_url=canonical_url,
            original_url=None if row[8] is None else str(row[8]),
            title=None if row[9] is None else str(row[9]),
            snippet=None if row[10] is None else str(row[10]),
            raw_item=dict(row[11] or {}),
            discovered_at=row[12],
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateOccurrenceRecord":
        if value.get("occurrence_id") is None:
            raise ValueError("candidate occurrence requires occurrence_id")
        if value.get("candidate_id") is None:
            raise ValueError("candidate occurrence requires candidate_id")
        return cls(
            occurrence_id=_uuid(value["occurrence_id"], field="occurrence_id"),
            candidate_id=_uuid(value["candidate_id"], field="candidate_id"),
            run_id=_uuid(value.get("run_id"), field="run_id"),
            search_response_id=_uuid(
                value.get("search_response_id"), field="search_response_id"
            ),
            plan_id=_optional_uuid(value.get("plan_id"), field="plan_id"),
            plan_query_id=_optional_uuid(
                value.get("plan_query_id"), field="plan_query_id"
            ),
            rank=int(value["rank"]),
            query_text=str(value["query_text"]),
            canonical_url=(
                None if value.get("canonical_url") is None else str(value["canonical_url"])
            ),
            original_url=(
                None if value.get("original_url") is None else str(value["original_url"])
            ),
            title=None if value.get("title") is None else str(value["title"]),
            snippet=None if value.get("snippet") is None else str(value["snippet"]),
            raw_item=dict(value.get("raw_item") or {}),
            discovered_at=value.get("discovered_at"),
            temporal_assessment=(
                None
                if value.get("temporal_assessment") is None
                else dict(value["temporal_assessment"])
            ),
            branches=tuple(str(item) for item in value.get("branches") or ()),
        )

    def with_temporal_assessment(
        self, assessment: Mapping[str, Any]
    ) -> "CandidateOccurrenceRecord":
        return replace(self, temporal_assessment=dict(assessment))

    def with_raw_item(self, raw_item: Mapping[str, Any]) -> "CandidateOccurrenceRecord":
        return replace(self, raw_item=dict(raw_item))

    def with_branches(self, branches: Sequence[str]) -> "CandidateOccurrenceRecord":
        return replace(self, branches=tuple(str(value) for value in branches))

    @property
    def identity_urls(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for value in (self.canonical_url, self.original_url)
                if value
            )
        )


@dataclass(frozen=True)
class ExtractedAssetRecord:
    """Canonical run-asset identity/provenance record used for resume/replay."""

    extraction_attempt_id: UUID
    candidate_id: UUID
    snapshot_id: UUID
    requested_url: str
    chunk_ids: tuple[UUID, ...]
    final_url: str | None
    canonical_url: str | None
    ordinal: int = 0
    status: str = "complete"
    resume_replay: bool = False

    @classmethod
    def from_repository_row(cls, row: Sequence[Any]) -> "ExtractedAssetRecord":
        if len(row) != 7:
            raise ValueError(
                f"run-asset repository row has {len(row)} fields; expected 7"
            )
        chunks = tuple(_uuid(value, field="chunk_id") for value in (row[4] or ()))
        if not chunks:
            raise ValueError("run-asset repository row requires chunk_ids")
        return cls(
            extraction_attempt_id=_uuid(row[0], field="extraction_attempt_id"),
            candidate_id=_uuid(row[1], field="candidate_id"),
            snapshot_id=_uuid(row[2], field="snapshot_id"),
            requested_url=str(row[3]),
            chunk_ids=chunks,
            final_url=None if row[5] is None else str(row[5]),
            canonical_url=None if row[6] is None else str(row[6]),
        )

    @classmethod
    def from_manifest_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        candidate_id: UUID,
        extraction_attempt_id: UUID,
    ) -> "ExtractedAssetRecord":
        canonical_candidate_id = _uuid(candidate_id, field="candidate_id")
        canonical_attempt_id = _uuid(
            extraction_attempt_id, field="extraction_attempt_id"
        )
        supplied_candidate = value.get("candidate_id")
        if supplied_candidate is not None and _uuid(
            supplied_candidate, field="candidate_id"
        ) != canonical_candidate_id:
            raise ValueError("conflicting extracted-asset candidate identity")
        supplied_attempt = value.get("extraction_attempt_id")
        if supplied_attempt is not None and _uuid(
            supplied_attempt, field="extraction_attempt_id"
        ) != canonical_attempt_id:
            raise ValueError("conflicting extracted-asset extraction attempt identity")
        snapshot_id = _uuid(value.get("snapshot_id"), field="snapshot_id")
        chunks = tuple(
            _uuid(item, field="chunk_id") for item in value.get("chunk_ids") or ()
        )
        if not chunks:
            raise ValueError("complete extracted asset requires chunk_ids")
        requested_url = value.get("requested_url")
        if not requested_url:
            raise ValueError("complete extracted asset requires requested_url")
        return cls(
            extraction_attempt_id=canonical_attempt_id,
            candidate_id=canonical_candidate_id,
            snapshot_id=snapshot_id,
            requested_url=str(requested_url),
            chunk_ids=chunks,
            final_url=(
                None if value.get("final_url") is None else str(value["final_url"])
            ),
            canonical_url=(
                None
                if value.get("canonical_url") is None
                else str(value["canonical_url"])
            ),
            ordinal=int(value.get("ordinal") or 0),
            status=str(value.get("status") or "complete"),
            resume_replay=bool(value.get("resume_replay", False)),
        )

    def for_resume(self, ordinal: int) -> "ExtractedAssetRecord":
        return replace(self, ordinal=ordinal, resume_replay=True)

    @property
    def identity_urls(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for value in (
                    self.requested_url,
                    self.final_url,
                    self.canonical_url,
                )
                if value
            )
        )


def candidate_record_to_public_dict(value: CandidateRecord) -> dict[str, Any]:
    """Preserve the existing public candidate JSON contract explicitly."""

    return {
        "id": value.candidate_id,
        "run_id": value.run_id,
        "canonical_url": value.canonical_url,
        "canonical_url_sha256": value.canonical_url_sha256,
        "original_url": value.original_url,
        "title": value.title,
        "snippet": value.snippet,
        "domain": value.domain,
        "backend": value.backend,
        "published_at": value.published_at,
        "date_signals": dict(value.date_signals),
        "backend_metadata": dict(value.backend_metadata),
        "recurrence_count": value.recurrence_count,
        "duplicate_group_id": value.duplicate_group_id,
        "first_seen_at": value.first_seen_at,
        "last_seen_at": value.last_seen_at,
        "created_at": value.created_at,
        "independence_assessment": (
            None
            if value.independence_assessment is None
            else dict(value.independence_assessment)
        ),
    }


def candidate_occurrence_to_public_dict(
    value: CandidateOccurrenceRecord,
) -> dict[str, Any]:
    """Preserve the existing public occurrence JSON contract explicitly."""

    result: dict[str, Any] = {
        "id": value.occurrence_id,
        "candidate_id": value.candidate_id,
        "run_id": value.run_id,
        "search_response_id": value.search_response_id,
        "plan_id": value.plan_id,
        "plan_query_id": value.plan_query_id,
        "rank": value.rank,
        "query_text": value.query_text,
        "original_url": value.original_url,
        "title": value.title,
        "snippet": value.snippet,
        "raw_item": dict(value.raw_item),
        "discovered_at": value.discovered_at,
    }
    if value.canonical_url is not None:
        result["canonical_url"] = value.canonical_url
    return result


def extracted_asset_to_dict(value: ExtractedAssetRecord) -> dict[str, Any]:
    return {
        "status": value.status,
        "ordinal": value.ordinal,
        "requested_url": value.requested_url,
        "final_url": value.final_url,
        "canonical_url": value.canonical_url,
        "snapshot_id": value.snapshot_id,
        "chunk_ids": list(value.chunk_ids),
        "candidate_id": value.candidate_id,
        "extraction_attempt_id": value.extraction_attempt_id,
        "resume_replay": value.resume_replay,
    }


__all__ = [
    "CandidateOccurrenceRecord",
    "CandidateRecord",
    "ExtractedAssetRecord",
    "candidate_occurrence_to_public_dict",
    "candidate_record_to_public_dict",
    "extracted_asset_to_dict",
]
