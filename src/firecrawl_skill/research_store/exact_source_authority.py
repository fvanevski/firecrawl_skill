"""Deterministic exact-source identity and bounded insufficiency contracts.

Issue #375 requires an explicitly named canonical resource to remain distinct
from a generic source class.  This module owns only deterministic identity
normalization and the typed gap used when that authority cannot be established;
it does not infer source equivalence from organization, domain, title, or model
judgment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from .read_models import (
    CandidateOccurrenceRecord,
    CandidateRecord,
    ExtractedAssetRecord,
)
from .url import canonicalize_url

_IDENTITY_KEYS = (
    "requested_url",
    "canonical_url",
    "final_url",
    "source_url",
    "url",
    "original_url",
)


def canonical_source_identity(value: Any) -> str | None:
    """Return the repository canonical identity for one absolute HTTP(S) URL."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return canonicalize_url(value)
    except (TypeError, ValueError):
        return None


def source_identity_aliases(
    value: Mapping[str, Any]
    | CandidateRecord
    | CandidateOccurrenceRecord
    | ExtractedAssetRecord,
) -> frozenset[str]:
    """Return bounded URL aliases explicitly carried by one workflow record.

    Typed repository reads expose their URLs explicitly.  Mapping lookup is
    retained only for passage payloads, which are a separate corpus projection.
    """

    if isinstance(
        value, (CandidateRecord, CandidateOccurrenceRecord, ExtractedAssetRecord)
    ):
        candidates = value.identity_urls
    else:
        candidates = tuple(value.get(key) for key in _IDENTITY_KEYS)
    return frozenset(
        identity
        for raw in candidates
        if (identity := canonical_source_identity(raw)) is not None
    )


def candidate_identity_map(
    assets: Sequence[
        CandidateRecord | CandidateOccurrenceRecord | ExtractedAssetRecord
    ],
    *,
    passages: list[dict[str, Any]] | None = None,
    chunk_to_candidate: Mapping[UUID, UUID] | None = None,
) -> dict[UUID, frozenset[str]]:
    """Build exact candidate -> proven canonical URL aliases."""

    aliases: dict[UUID, set[str]] = {}
    for asset in assets:
        aliases.setdefault(asset.candidate_id, set()).update(
            source_identity_aliases(asset)
        )

    if passages and chunk_to_candidate:
        for passage in passages:
            chunk_id = UUID(str(passage["chunk_id"]))
            candidate_id = chunk_to_candidate.get(chunk_id)
            if candidate_id is None:
                continue
            aliases.setdefault(candidate_id, set()).update(
                source_identity_aliases(passage)
            )

    return {candidate_id: frozenset(values) for candidate_id, values in aliases.items()}


_AUTHORITATIVE_RELATIONSHIP_BY_STATUS = {
    "supported": "supports",
    "contradicted": "contradicts",
    "qualified": "qualifies",
}


def exact_source_binding_is_authoritative(
    semantic_status: object,
    relationship: object,
) -> bool:
    """Return whether a final binding can discharge exact-source authority."""

    status_value = getattr(semantic_status, "value", semantic_status)
    relationship_value = getattr(relationship, "value", relationship)
    return _AUTHORITATIVE_RELATIONSHIP_BY_STATUS.get(str(status_value)) == str(
        relationship_value
    )


def requirement_candidate_groups(
    requirements: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    identities: Mapping[UUID, frozenset[str]],
) -> dict[str, frozenset[UUID]]:
    """Resolve each structured requirement to candidates proving that identity."""

    groups: dict[str, frozenset[UUID]] = {}
    for requirement in requirements:
        requirement_id = str(requirement["requirement_id"])
        expected = canonical_source_identity(requirement.get("canonical_url"))
        if expected is None:
            raise ValueError(
                f"exact-source requirement {requirement_id} has invalid canonical_url"
            )
        groups[requirement_id] = frozenset(
            candidate_id
            for candidate_id, aliases in identities.items()
            if expected in aliases
        )
    return groups


@dataclass(frozen=True)
class ExactSourceRequirementState:
    """Bounded state for one exact-source obligation at the evidence boundary."""

    requirement_id: str
    canonical_url: str
    candidate_ids: tuple[str, ...] = ()
    acquired: bool = False
    selected: bool = False
    satisfied: bool = False
    passage_ids: tuple[str, ...] = ()
    reason: str = "unresolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "canonical_url": self.canonical_url,
            "candidate_ids": list(self.candidate_ids),
            "acquired": self.acquired,
            "selected": self.selected,
            "satisfied": self.satisfied,
            "passage_ids": list(self.passage_ids),
            "reason": self.reason,
        }


class ExactSourceCoverageUnsatisfied(RuntimeError):
    """An explicit exact-source authority obligation remains unsatisfied."""

    def __init__(self, states: tuple[ExactSourceRequirementState, ...]) -> None:
        if not states:
            raise ValueError("exact-source coverage gap requires at least one state")
        self.states = states
        reasons = ", ".join(sorted({state.reason for state in states}))
        super().__init__(f"exact canonical-source authority unsatisfied: {reasons}")

    def to_gap(self, *, coverage_revision: int | None) -> dict[str, Any]:
        return {
            "kind": "exact_source_coverage_gap",
            "status": "unsatisfied",
            "recoverable": True,
            "automatic_scope_relaxation": False,
            "coverage_revision": coverage_revision,
            "required_resolution": "acquire_or_bind_required_exact_source",
            "requirements": [state.to_dict() for state in self.states],
        }


__all__ = [
    "ExactSourceCoverageUnsatisfied",
    "ExactSourceRequirementState",
    "candidate_identity_map",
    "canonical_source_identity",
    "exact_source_binding_is_authoritative",
    "requirement_candidate_groups",
    "source_identity_aliases",
]
