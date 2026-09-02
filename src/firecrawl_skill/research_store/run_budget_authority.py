"""Persisted run authority joining planning and candidate extraction budgets.

Planning resource caps and candidate/corpus policy remain distinct authorities.
This module binds only their overlapping hard extraction-attempt dimension into
one immutable run snapshot so planned acquisition and later candidate-policy
checks cannot reinterpret that limit from process-local configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from .acquisition.candidate_ranking import CandidateBudget
from .budget_policy import ResourceCaps

CANDIDATE_BUDGET_KEY = "candidate_budget"
PLANNED_EXTRACTION_AUTHORITY_KEY = "planned_acquisition_extraction_authority"
PLANNED_EXTRACTION_AUTHORITY_SCHEMA = "planned-acquisition-extraction-authority-v1"


def _resource_caps(snapshot: Mapping[str, Any]) -> ResourceCaps:
    raw_caps = snapshot.get("effective_caps")
    if not isinstance(raw_caps, Mapping):
        raise ValueError("persisted planning budget has no effective_caps")
    return ResourceCaps.from_mapping(dict(raw_caps))


def load_persisted_candidate_budget(snapshot: Mapping[str, Any]) -> CandidateBudget:
    """Load an exact candidate-budget snapshot without applying dataclass defaults."""

    raw_budget = snapshot.get(CANDIDATE_BUDGET_KEY)
    if not isinstance(raw_budget, Mapping):
        raise ValueError("persisted planning budget has no candidate_budget authority")
    expected = {item.name for item in fields(CandidateBudget)}
    present = set(raw_budget)
    missing = sorted(expected - present)
    unknown = sorted(present - expected)
    if missing or unknown:
        raise ValueError(
            f"invalid persisted candidate budget; missing={missing}, unknown={unknown}"
        )
    return CandidateBudget(**{name: raw_budget[name] for name in expected})


def bind_planned_acquisition_budget_authority(
    planning_snapshot: Mapping[str, Any],
    candidate_budget: CandidateBudget,
) -> dict[str, Any]:
    """Bind configured candidate policy and the stricter overlapping hard cap."""

    if not isinstance(candidate_budget, CandidateBudget):
        raise TypeError("candidate_budget must be CandidateBudget")
    snapshot = dict(planning_snapshot)
    caps = _resource_caps(snapshot)
    candidate_limit = candidate_budget.max_exploratory_extraction_attempts
    effective_limit = min(caps.max_extraction_attempts, candidate_limit)
    snapshot[CANDIDATE_BUDGET_KEY] = candidate_budget.to_dict()
    snapshot[PLANNED_EXTRACTION_AUTHORITY_KEY] = {
        "schema_version": PLANNED_EXTRACTION_AUTHORITY_SCHEMA,
        "planning_max_extraction_attempts": caps.max_extraction_attempts,
        "candidate_max_exploratory_extraction_attempts": candidate_limit,
        "effective_max_extraction_attempts": effective_limit,
    }
    return snapshot


def load_planned_extraction_attempt_limit(snapshot: Mapping[str, Any]) -> int:
    """Validate and return the persisted reconciled planned-acquisition hard cap."""

    caps = _resource_caps(snapshot)
    candidate_budget = load_persisted_candidate_budget(snapshot)
    raw_authority = snapshot.get(PLANNED_EXTRACTION_AUTHORITY_KEY)
    if not isinstance(raw_authority, Mapping):
        raise ValueError(
            "persisted planning budget has no planned acquisition extraction authority"
        )
    expected_keys = {
        "schema_version",
        "planning_max_extraction_attempts",
        "candidate_max_exploratory_extraction_attempts",
        "effective_max_extraction_attempts",
    }
    present = set(raw_authority)
    if present != expected_keys:
        raise ValueError(
            "invalid planned acquisition extraction authority; "
            f"missing={sorted(expected_keys - present)}, "
            f"unknown={sorted(present - expected_keys)}"
        )
    if raw_authority.get("schema_version") != PLANNED_EXTRACTION_AUTHORITY_SCHEMA:
        raise ValueError("unsupported planned acquisition extraction authority schema")

    planning_limit = raw_authority.get("planning_max_extraction_attempts")
    candidate_limit = raw_authority.get("candidate_max_exploratory_extraction_attempts")
    effective_limit = raw_authority.get("effective_max_extraction_attempts")
    for name, value in (
        ("planning_max_extraction_attempts", planning_limit),
        ("candidate_max_exploratory_extraction_attempts", candidate_limit),
        ("effective_max_extraction_attempts", effective_limit),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"persisted {name} must be a non-negative integer")

    if planning_limit != caps.max_extraction_attempts:
        raise ValueError(
            "persisted planned acquisition authority contradicts planning resource caps"
        )
    if candidate_limit != candidate_budget.max_exploratory_extraction_attempts:
        raise ValueError(
            "persisted planned acquisition authority contradicts candidate budget"
        )
    if effective_limit != min(planning_limit, candidate_limit):
        raise ValueError(
            "persisted planned acquisition effective extraction cap is not the stricter hard cap"
        )
    return effective_limit


__all__ = [
    "CANDIDATE_BUDGET_KEY",
    "PLANNED_EXTRACTION_AUTHORITY_KEY",
    "PLANNED_EXTRACTION_AUTHORITY_SCHEMA",
    "bind_planned_acquisition_budget_authority",
    "load_persisted_candidate_budget",
    "load_planned_extraction_attempt_limit",
]
