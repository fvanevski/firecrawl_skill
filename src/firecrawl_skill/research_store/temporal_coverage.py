"""Deterministic diagnostics for temporal evidence coverage.

This module classifies why authoritative passages cannot satisfy a persisted
ResearchSpec. It never changes the spec, treats retrieval time as non-authority,
and uses the same temporal policy as evidence qualification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .temporal_policy import (
    freshness_satisfied,
    has_temporal_obligations,
    normalize_temporal,
    passage_temporally_qualifies,
    publication_in_window,
)


@dataclass(frozen=True)
class TemporalCoverageDiagnostics:
    """Bounded count census explaining a temporal coverage disposition."""

    basis: str
    examined_passages: int
    qualifying_passages: int
    missing_publication_authority: int = 0
    unparsable_publication_authority: int = 0
    future_publication_authority: int = 0
    publication_out_of_window: int = 0
    missing_freshness_authority: int = 0
    unparsable_update_authority: int = 0
    future_freshness_authority: int = 0
    stale_freshness_authority: int = 0
    retrieval_only_passages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TemporalCoverageUnsatisfied(RuntimeError):
    """Typed evidence-boundary signal for recoverable temporal insufficiency.

    This class deliberately does not inherit from the generic evidence-preparation
    error. Smart resume catches this exact type; unrelated preparation failures
    therefore cannot be reclassified merely because current passages also fail a
    temporal predicate.
    """

    def __init__(self, diagnostics: TemporalCoverageDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "bounded ResearchSpec has no temporally qualifying authoritative passages"
        )

    def to_gap(self, *, coverage_revision: int | None) -> dict[str, Any]:
        return temporal_gap_payload(
            self.diagnostics,
            coverage_revision=coverage_revision,
        )


def temporal_basis(spec: Mapping[str, Any]) -> str:
    window = spec.get("time_window") or {}
    publication_required = isinstance(window, Mapping) and bool(
        window.get("start") or window.get("end")
    )
    freshness_required = any(
        isinstance(item, Mapping) and item.get("max_age_days") is not None
        for item in spec.get("freshness_requirements", ())
    )
    if publication_required and freshness_required:
        return "conjunctive"
    if publication_required:
        return "publication_window"
    if freshness_required:
        return "freshness"
    return "none"


def diagnose_temporal_coverage(
    passages: Sequence[Mapping[str, Any]],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> TemporalCoverageDiagnostics:
    """Classify temporal failures without inferring authority from generic dates."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    basis = temporal_basis(spec)
    if basis == "none":
        return TemporalCoverageDiagnostics(
            basis=basis,
            examined_passages=len(passages),
            qualifying_passages=len(passages),
        )

    window = spec.get("time_window") or {}
    publication_required = basis in {"publication_window", "conjunctive"}
    freshness_ages = [
        int(item["max_age_days"])
        for item in spec.get("freshness_requirements", ())
        if isinstance(item, Mapping) and item.get("max_age_days") is not None
    ]

    counts = {
        "qualifying_passages": 0,
        "missing_publication_authority": 0,
        "unparsable_publication_authority": 0,
        "future_publication_authority": 0,
        "publication_out_of_window": 0,
        "missing_freshness_authority": 0,
        "unparsable_update_authority": 0,
        "future_freshness_authority": 0,
        "stale_freshness_authority": 0,
        "retrieval_only_passages": 0,
    }

    for passage in passages:
        if passage_temporally_qualifies(passage, spec, now=reference):
            counts["qualifying_passages"] += 1
            continue

        publication_raw = passage.get("published_at")
        update_raw = passage.get("updated_at") or passage.get("last_modified")
        publication = normalize_temporal(publication_raw)
        update = normalize_temporal(update_raw)

        if publication_required:
            if publication_raw in (None, ""):
                counts["missing_publication_authority"] += 1
            elif publication is None:
                counts["unparsable_publication_authority"] += 1
            elif publication > reference:
                counts["future_publication_authority"] += 1
            elif not publication_in_window(publication, window, now=reference):
                counts["publication_out_of_window"] += 1

        if freshness_ages:
            freshness_ok = all(
                freshness_satisfied(
                    published_at=publication,
                    updated_at=update,
                    max_age_days=max_age,
                    now=reference,
                )
                for max_age in freshness_ages
            )
            if not freshness_ok:
                if publication_raw in (None, "") and update_raw in (None, ""):
                    counts["missing_freshness_authority"] += 1
                if update_raw not in (None, "") and update is None:
                    counts["unparsable_update_authority"] += 1
                values = [value for value in (publication, update) if value is not None]
                if any(value > reference for value in values):
                    counts["future_freshness_authority"] += 1
                non_future = [value for value in values if value <= reference]
                if non_future:
                    oldest_allowed = reference - timedelta(days=min(freshness_ages))
                    if all(value < oldest_allowed for value in non_future):
                        counts["stale_freshness_authority"] += 1

        if (
            publication is None
            and update is None
            and passage.get("retrieved_at") not in (None, "")
        ):
            counts["retrieval_only_passages"] += 1

    return TemporalCoverageDiagnostics(
        basis=basis,
        examined_passages=len(passages),
        **counts,
    )


def temporal_gap_payload(
    diagnostics: TemporalCoverageDiagnostics,
    *,
    coverage_revision: int | None,
) -> dict[str, Any]:
    """Return the stable persisted/operator-facing recoverable gap contract."""

    return {
        "kind": "temporal_coverage_gap",
        "status": "unsatisfied",
        "recoverable": True,
        "coverage_revision": coverage_revision,
        "diagnostics": diagnostics.to_dict(),
        "automatic_scope_relaxation": False,
        "scope_relaxation_requires": "persisted_research_spec_revision",
        "required_resolution": (
            "acquire_temporally_qualifying_authoritative_evidence_or_"
            "persist_an_explicit_research_spec_revision"
        ),
    }


def should_classify_temporal_gap(
    passages: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
) -> bool:
    """Cheap predicate used only by bounded diagnostic/inspection helpers."""

    return bool(passages) and has_temporal_obligations(spec)


__all__ = [
    "TemporalCoverageDiagnostics",
    "TemporalCoverageUnsatisfied",
    "diagnose_temporal_coverage",
    "should_classify_temporal_gap",
    "temporal_basis",
    "temporal_gap_payload",
]
