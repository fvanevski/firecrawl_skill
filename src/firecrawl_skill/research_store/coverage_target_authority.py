"""Canonical semantic targets for durable coverage items.

Coverage rows intentionally carry progress/state rather than duplicated semantic
text.  Whenever semantic work needs to understand what a coverage item means,
its subject is resolved back to the persisted ResearchSpec instead of trusting a
lossy transient coverage projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_TARGET_FIELDS: dict[str, tuple[str, str, str]] = {
    "question": ("questions", "question_id", "text"),
    "claim": ("claims_to_validate", "claim_id", "statement"),
    "source_requirement": (
        "required_source_classes",
        "requirement_id",
        "source_class",
    ),
    "exact_source_requirement": (
        "exact_source_requirements",
        "requirement_id",
        "canonical_url",
    ),
    "freshness_requirement": (
        "freshness_requirements",
        "requirement_id",
        "description",
    ),
    "corroboration_requirement": (
        "corroboration_requirements",
        "requirement_id",
        "description",
    ),
    "contradiction_requirement": (
        "contradiction_requirements",
        "requirement_id",
        "description",
    ),
}


class CoverageTargetAuthorityError(ValueError):
    """A coverage subject cannot be resolved to its persisted semantic authority."""


def coverage_target_text(
    spec: Mapping[str, Any],
    item_type: str,
    subject_id: object,
) -> str:
    """Resolve one coverage subject to its canonical ResearchSpec text."""

    contract = _TARGET_FIELDS.get(str(item_type))
    if contract is None:
        raise CoverageTargetAuthorityError(
            f"unsupported coverage item type for semantic authority: {item_type!r}"
        )
    collection_name, identity_field, text_field = contract
    identity = str(subject_id or "").strip()
    if not identity:
        raise CoverageTargetAuthorityError("coverage item has no subject identity")
    values = spec.get(collection_name)
    if not isinstance(values, (list, tuple)):
        raise CoverageTargetAuthorityError(
            f"ResearchSpec field {collection_name!r} is not a collection"
        )
    matches = [
        value
        for value in values
        if isinstance(value, Mapping)
        and str(value.get(identity_field) or "") == identity
    ]
    if len(matches) != 1:
        raise CoverageTargetAuthorityError(
            f"coverage subject {item_type}:{identity} resolves to {len(matches)} ResearchSpec records"
        )
    text = " ".join(str(matches[0].get(text_field) or "").split())
    if not text:
        raise CoverageTargetAuthorityError(
            f"coverage subject {item_type}:{identity} has no semantic text in ResearchSpec"
        )
    return text


def semantic_coverage_item(
    spec: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind transient coverage state to canonical ResearchSpec semantic meaning."""

    item_type = str(item.get("item_type") or "")
    return {
        **dict(item),
        "text": coverage_target_text(spec, item_type, item.get("subject_id")),
    }


__all__ = [
    "CoverageTargetAuthorityError",
    "coverage_target_text",
    "semantic_coverage_item",
]
