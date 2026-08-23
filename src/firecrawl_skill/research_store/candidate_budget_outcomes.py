"""Typed completion-admission outcomes at the smart-run orchestration boundary.

The authoritative candidate-budget decision remains the persisted PostgreSQL
``corpus_budget_checks`` row.  This module classifies that row without parsing
human-readable exception text so orchestration can distinguish a hard rejection
from an exact-scope soft override requirement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class CandidateBudgetAdmissionContext:
    run_id: UUID
    lifecycle_revision: int
    check_id: UUID
    scope: Mapping[str, Any]
    scope_fingerprint: str
    violated_limits: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "lifecycle_revision": self.lifecycle_revision,
            "check_id": str(self.check_id),
            "scope": dict(self.scope),
            "scope_fingerprint": self.scope_fingerprint,
            "violated_limits": list(self.violated_limits),
        }


class CandidateBudgetAdmissionBoundaryError(RuntimeError):
    """Base type for exact persisted completion-admission failures."""

    outcome = "candidate_budget_rejected"

    def __init__(self, context: CandidateBudgetAdmissionContext) -> None:
        self.context = context
        super().__init__(
            f"{self.outcome}: check={context.check_id}; "
            f"limits={','.join(context.violated_limits)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.outcome, **self.context.to_dict()}


class CandidateBudgetOverrideRequired(CandidateBudgetAdmissionBoundaryError):
    """An exact completion set is blocked only by unresolved soft limits."""

    outcome = "candidate_budget_override_required"


class CandidateBudgetHardRejected(CandidateBudgetAdmissionBoundaryError):
    """An exact completion set violates one or more non-overridable limits."""

    outcome = "candidate_budget_hard_rejected"


def _limit_names(rows: Any) -> set[str]:
    if not isinstance(rows, list):
        return set()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            value = row.get("limit_name")
            if value is not None and str(value).strip():
                names.add(str(value))
    return names


def classify_persisted_completion_admission(
    policy: Any,
    run_id: UUID,
    lifecycle_revision: int,
) -> CandidateBudgetAdmissionBoundaryError | None:
    """Classify the latest exact-revision persisted completion check, if blocked.

    The function deliberately reads the authoritative check record instead of
    inspecting an ``AssetPromotionError`` string.  An accepted check returns
    ``None``; unresolved hard limits take precedence over soft limits.
    """

    matching = [
        check
        for check in policy.list_checks(run_id)
        if check.get("phase") == "completion_admission"
        and int(check.get("lifecycle_revision") or -1) == lifecycle_revision
    ]
    if not matching:
        return None
    check = matching[-1]
    hard = _limit_names(check.get("hard_violations"))
    soft = _limit_names(check.get("soft_violations"))
    overridden = {str(value) for value in check.get("overridden_limits", ())}
    unresolved_soft = soft - overridden
    context = CandidateBudgetAdmissionContext(
        run_id=run_id,
        lifecycle_revision=lifecycle_revision,
        check_id=UUID(str(check["id"])),
        scope=dict(check.get("scope") or {}),
        scope_fingerprint=str(check.get("content_sha256") or ""),
        violated_limits=tuple(sorted(hard or unresolved_soft)),
    )
    if hard:
        return CandidateBudgetHardRejected(context)
    if unresolved_soft:
        return CandidateBudgetOverrideRequired(context)
    return None


__all__ = [
    "CandidateBudgetAdmissionBoundaryError",
    "CandidateBudgetAdmissionContext",
    "CandidateBudgetHardRejected",
    "CandidateBudgetOverrideRequired",
    "classify_persisted_completion_admission",
]
