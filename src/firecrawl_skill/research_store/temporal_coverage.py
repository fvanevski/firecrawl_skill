"""Deterministic diagnostics for temporal evidence coverage.

This module classifies why authoritative passages cannot satisfy a persisted
ResearchSpec. It never changes the spec, treats retrieval time as non-authority,
and uses the same typed temporal policy as evidence qualification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .temporal_policy import (
    has_temporal_obligations,
    passage_temporal_qualification,
    resolved_temporal_basis,
)


@dataclass(frozen=True)
class TemporalCoverageDiagnostics:
    """Bounded census explaining an obligation-specific temporal disposition."""

    basis: str
    examined_passages: int
    qualifying_passages: int
    violating_passages: int = 0
    unresolved_passages: int = 0
    missing_publication_authority: int = 0
    missing_update_authority: int = 0
    missing_freshness_authority: int = 0
    publication_out_of_window: int = 0
    stale_freshness_authority: int = 0
    event_time_unresolved: int = 0
    event_out_of_window: int = 0
    as_of_state_unresolved: int = 0
    as_of_state_out_of_window: int = 0
    invalid_or_conflicting_authority: int = 0
    provenance_resolution_exhausted: int = 0
    retrieval_only_passages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TemporalCoverageUnsatisfied(RuntimeError):
    """Typed evidence-boundary signal for recoverable temporal insufficiency."""

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
    """Compatibility-facing name for the current typed ResearchSpec basis."""

    return resolved_temporal_basis(spec)


def _resolution_exhausted(passage: Mapping[str, Any]) -> bool:
    provenance = passage.get("temporal_provenance")
    if not isinstance(provenance, Mapping):
        return False
    resolution = provenance.get("resolution")
    return isinstance(resolution, Mapping) and resolution.get("exhausted") is True


def diagnose_temporal_coverage(
    passages: Sequence[Mapping[str, Any]],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> TemporalCoverageDiagnostics:
    """Classify satisfies/violates/unresolved without fabricating time authority."""

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

    counts = {
        "qualifying_passages": 0,
        "violating_passages": 0,
        "unresolved_passages": 0,
        "missing_publication_authority": 0,
        "missing_update_authority": 0,
        "missing_freshness_authority": 0,
        "publication_out_of_window": 0,
        "stale_freshness_authority": 0,
        "event_time_unresolved": 0,
        "event_out_of_window": 0,
        "as_of_state_unresolved": 0,
        "as_of_state_out_of_window": 0,
        "invalid_or_conflicting_authority": 0,
        "provenance_resolution_exhausted": 0,
        "retrieval_only_passages": 0,
    }

    for passage in passages:
        qualification = passage_temporal_qualification(passage, spec, now=reference)
        if qualification.status == "satisfies":
            counts["qualifying_passages"] += 1
            continue
        counts[
            "violating_passages"
            if qualification.status == "violates"
            else "unresolved_passages"
        ] += 1

        reason = qualification.reason
        if reason == "missing_publication_authority":
            counts["missing_publication_authority"] += 1
        elif reason == "missing_update_authority":
            counts["missing_update_authority"] += 1
        elif reason == "missing_publication_or_update_authority":
            counts["missing_publication_authority"] += 1
            counts["missing_update_authority"] += 1
            counts["missing_freshness_authority"] += 1
        elif reason == "explicit_publication_out_of_window":
            counts["publication_out_of_window"] += 1
        elif reason in {
            "authoritative_publication_and_update_are_stale_or_future",
            "authoritative_publication_and_update_out_of_window",
        }:
            counts["stale_freshness_authority"] += 1
        elif reason == "event_time_unresolved":
            counts["event_time_unresolved"] += 1
        elif reason == "authoritative_event_out_of_window":
            counts["event_out_of_window"] += 1
        elif reason == "as_of_state_unresolved":
            counts["as_of_state_unresolved"] += 1
        elif reason == "authoritative_state_interval_excludes_as_of":
            counts["as_of_state_out_of_window"] += 1
        if "invalid_or_conflicting" in reason:
            counts["invalid_or_conflicting_authority"] += 1

        if _resolution_exhausted(passage):
            counts["provenance_resolution_exhausted"] += 1
        provenance = passage.get("temporal_provenance")
        published = passage.get("published_at")
        updated = passage.get("updated_at") or passage.get("last_modified")
        event_at = (
            provenance.get("event_at") if isinstance(provenance, Mapping) else None
        )
        if (
            published in (None, "")
            and updated in (None, "")
            and event_at in (None, "")
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

    qualification_state = (
        "unresolved" if diagnostics.unresolved_passages else "violates"
    )
    required_resolution = (
        "resolve_bounded_temporal_provenance_or_acquire_qualifying_evidence"
        if qualification_state == "unresolved"
        else "acquire_temporally_qualifying_authoritative_evidence"
    )
    return {
        "kind": "temporal_coverage_gap",
        "status": qualification_state,
        "recoverable": True,
        "coverage_revision": coverage_revision,
        "diagnostics": diagnostics.to_dict(),
        "automatic_scope_relaxation": False,
        "scope_relaxation_requires": "persisted_research_spec_revision",
        "required_resolution": required_resolution,
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
