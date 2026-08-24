"""Deterministic ResearchSpec-based temporal assessment of search candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .temporal_candidate import parse_provider_datetime
from .temporal_coverage import temporal_basis
from .temporal_policy import freshness_satisfied, publication_in_window


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
    """Return eligible/ineligible/unknown without consulting generic provider dates."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
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

    def result(status: str, reason: str) -> CandidateTemporalAssessment:
        return CandidateTemporalAssessment(
            status=status,
            basis=basis,
            reason=reason,
            published_at=publication.isoformat() if publication is not None else None,
            updated_at=update.isoformat() if update is not None else None,
            publication_status=publication_status,
            update_status=update_status,
        )

    if basis == "none":
        return result("eligible", "ResearchSpec has no temporal evidence obligation")

    publication_state = "not_required"
    if basis in {"publication_window", "conjunctive"}:
        if publication is None:
            publication_state = "unknown"
        elif publication > reference:
            publication_state = "ineligible"
        elif publication_in_window(publication, spec.get("time_window") or {}, now=reference):
            publication_state = "eligible"
        else:
            publication_state = "ineligible"

    freshness_state = "not_required"
    if basis in {"freshness", "conjunctive"}:
        ages = [
            int(item["max_age_days"])
            for item in spec.get("freshness_requirements", ())
            if isinstance(item, Mapping) and item.get("max_age_days") is not None
        ]
        if publication is None and update is None:
            freshness_state = "unknown"
        elif all(
            freshness_satisfied(
                published_at=publication,
                updated_at=update,
                max_age_days=age,
                now=reference,
            )
            for age in ages
        ):
            freshness_state = "eligible"
        else:
            freshness_state = "ineligible"

    states = {publication_state, freshness_state} - {"not_required"}
    if "ineligible" in states:
        return result(
            "ineligible",
            "known explicit temporal authority cannot satisfy the ResearchSpec",
        )
    if "unknown" in states:
        return result(
            "unknown",
            "candidate lacks sufficient explicit temporal authority for pre-scrape proof",
        )
    return result("eligible", "explicit temporal authority satisfies the ResearchSpec")


__all__ = ["CandidateTemporalAssessment", "assess_candidate_temporal"]
