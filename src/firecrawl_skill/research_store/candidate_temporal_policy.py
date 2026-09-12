"""Deterministic ResearchSpec-based temporal assessment of search candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .temporal_candidate import parse_provider_datetime
from .temporal_coverage import temporal_basis
from .temporal_policy import passage_temporal_qualification


@dataclass(frozen=True)
class CandidateTemporalAssessment:
    status: str
    basis: str
    reason: str
    published_at: str | None
    updated_at: str | None
    publication_status: str
    update_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _known_explicit(status: str) -> bool:
    return status in {"explicit_provider_valid", "previous_explicit_provider"}


def assess_candidate_temporal(
    candidate: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> CandidateTemporalAssessment:
    """Return eligible/ineligible/unknown without consulting generic provider dates.

    Candidate metadata can establish publication/update authority before scrape,
    but event-time and as-of state normally remain unresolved until acquired
    source provenance is inspected. The three-state evidence policy is reused so
    missing update authority is never collapsed into a known stale verdict.
    """

    basis = temporal_basis(spec)
    signals = candidate.get("date_signals") or {}
    if not isinstance(signals, Mapping):
        signals = {}
    publication_status = str(signals.get("publication_status") or "unknown")
    update_status = str(signals.get("update_status") or "unknown")
    publication = (
        parse_provider_datetime(candidate.get("published_at"))
        if _known_explicit(publication_status)
        else None
    )
    update = (
        parse_provider_datetime(signals.get("updated_date"))
        if _known_explicit(update_status)
        else None
    )

    qualification = passage_temporal_qualification(
        {
            "published_at": publication,
            "updated_at": update,
            "temporal_provenance": {
                "publication_status": publication_status,
                "update_status": update_status,
            },
        },
        spec,
        now=now,
    )
    status = {
        "satisfies": "eligible",
        "violates": "ineligible",
        "unresolved": "unknown",
    }[qualification.status]
    return CandidateTemporalAssessment(
        status=status,
        basis=basis,
        reason=qualification.reason,
        published_at=publication.isoformat() if publication is not None else None,
        updated_at=update.isoformat() if update is not None else None,
        publication_status=publication_status,
        update_status=update_status,
    )


__all__ = ["CandidateTemporalAssessment", "assess_candidate_temporal"]
