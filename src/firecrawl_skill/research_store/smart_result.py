"""Canonical smart-run result, attempt census, and CLI disposition contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .orchestrator import OrchestratorResult

SMART_RESUMABLE_EXIT = 75
SMART_FAILURE_EXIT = 1
SMART_SUCCESS_EXIT = 0


@dataclass(frozen=True)
class AcquisitionAttemptCensus:
    attempted: int
    succeeded: int
    unsuccessful: int
    failure_counts: dict[str, int] = field(default_factory=dict)
    unsuccessful_attempts: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.attempted != self.succeeded + self.unsuccessful:
            raise ValueError(
                "attempt census must conserve attempted=succeeded+unsuccessful"
            )


@dataclass(frozen=True)
class OperatorActionOrchestratorResult(OrchestratorResult):
    """Nonterminal result carrying a typed operator-action contract."""

    operator_action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["operator_action"] = (
            dict(self.operator_action) if self.operator_action is not None else None
        )
        return payload


@dataclass(frozen=True)
class SmartOrchestratorResult(OrchestratorResult):
    """Orchestrator result plus PostgreSQL-derived extraction-attempt census."""

    attempted_urls: int = 0
    successful_attempts: int = 0
    unsuccessful_urls: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    unsuccessful_attempts: tuple[dict[str, Any], ...] = ()
    operator_action: dict[str, Any] | None = None

    @classmethod
    def from_result(
        cls,
        result: OrchestratorResult,
        census: AcquisitionAttemptCensus,
    ) -> SmartOrchestratorResult:
        action = getattr(result, "operator_action", None)
        return cls(
            run_id=result.run_id,
            final_state=result.final_state,
            outcome=result.outcome,
            coverage_revision=result.coverage_revision,
            wave_count=result.wave_count,
            successful_urls=result.successful_urls,
            strategy_proposals=result.strategy_proposals,
            strategy_decisions=result.strategy_decisions,
            error=result.error,
            attempted_urls=census.attempted,
            successful_attempts=census.succeeded,
            unsuccessful_urls=census.unsuccessful,
            failure_counts=dict(sorted(census.failure_counts.items())),
            unsuccessful_attempts=census.unsuccessful_attempts,
            operator_action=dict(action) if isinstance(action, dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "attempted_urls": self.attempted_urls,
                "successful_attempts": self.successful_attempts,
                "unsuccessful_urls": self.unsuccessful_urls,
                "failure_counts": dict(self.failure_counts),
                "unsuccessful_attempts": [
                    dict(item) for item in self.unsuccessful_attempts
                ],
                "operator_action": (
                    dict(self.operator_action)
                    if self.operator_action is not None
                    else None
                ),
            }
        )
        return payload


@dataclass(frozen=True)
class SmartCliDisposition:
    exit_code: int
    next_action: str


def _operator_action_next_action(result: OrchestratorResult) -> str:
    action = getattr(result, "operator_action", None)
    kind = action.get("kind") if isinstance(action, dict) else None
    if kind == "candidate_budget_override_required":
        return "resolve_candidate_budget_override_then_resume_same_run"
    if kind == "temporal_coverage_gap":
        return "resolve_temporal_coverage_gap_then_resume_same_run"
    return "inspect_operator_action_then_resume_same_run"


def smart_cli_disposition(result: OrchestratorResult) -> SmartCliDisposition:
    """Map one canonical workflow result to process status and next action.

    ``partial`` remains a successful *terminal* workflow result under the
    existing terminal contract. Recoverable work is explicitly non-success at
    the process boundary even when no error occurred.
    """

    outcome = str(result.outcome)
    state = str(result.final_state)
    if outcome in {"checkpoint", "resumable", "operator_action_required"}:
        next_action = (
            _operator_action_next_action(result)
            if outcome == "operator_action_required"
            else "resume_same_run"
        )
        return SmartCliDisposition(SMART_RESUMABLE_EXIT, next_action)
    if state == "completed" and outcome == "completed" and result.error is None:
        return SmartCliDisposition(SMART_SUCCESS_EXIT, "none")
    if state == "partial" and outcome == "partial" and result.error is None:
        return SmartCliDisposition(SMART_SUCCESS_EXIT, "terminal_partial")
    if state in {"failed", "cancelled"} or outcome in {"failed", "cancelled"}:
        return SmartCliDisposition(SMART_FAILURE_EXIT, "inspect_terminal_run")
    if result.error is not None:
        return SmartCliDisposition(SMART_FAILURE_EXIT, "inspect_run_error")
    return SmartCliDisposition(SMART_FAILURE_EXIT, "inspect_unrecognized_result")


def format_attempt_census(result: SmartOrchestratorResult) -> str:
    """Stable operator summary; no persistence reads or workflow decisions."""
    summary = (
        f"Attempt census: {result.successful_attempts} of "
        f"{result.attempted_urls} succeeded; "
        f"{result.unsuccessful_urls} unsuccessful"
    )
    if result.failure_counts:
        failures = ", ".join(
            f"{name}={count}" for name, count in sorted(result.failure_counts.items())
        )
        summary += f"; {failures}"
    return summary


def format_temporal_disposition(result: OrchestratorResult) -> str | None:
    """Return a bounded temporal-gap summary without expanding attempt details."""

    action = getattr(result, "operator_action", None)
    if not isinstance(action, dict) or action.get("kind") != "temporal_coverage_gap":
        return None
    diagnostics = action.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return "Temporal coverage: unsatisfied; diagnostics unavailable"
    reasons = []
    reason_fields = (
        ("missing_publication", "missing_publication_authority"),
        ("unparsable_publication", "unparsable_publication_authority"),
        ("future_publication", "future_publication_authority"),
        ("publication_out_of_window", "publication_out_of_window"),
        ("missing_freshness", "missing_freshness_authority"),
        ("unparsable_update", "unparsable_update_authority"),
        ("future_freshness", "future_freshness_authority"),
        ("stale_freshness", "stale_freshness_authority"),
        ("retrieval_only", "retrieval_only_passages"),
    )
    for label, metric in reason_fields:
        value = diagnostics.get(metric, 0)
        if isinstance(value, int) and value > 0:
            reasons.append(f"{label}={value}")
    reason_summary = ",".join(reasons) if reasons else "none-recorded"
    relaxation = (
        "false" if action.get("automatic_scope_relaxation") is False else "unknown"
    )
    required_resolution = str(
        action.get("required_resolution") or "inspect_temporal_coverage_gap"
    )
    return (
        "Temporal coverage: unsatisfied; "
        f"basis={diagnostics.get('basis', 'unknown')}; "
        f"qualifying={diagnostics.get('qualifying_passages', 0)}/"
        f"{diagnostics.get('examined_passages', 0)}; "
        f"reasons={reason_summary}; "
        f"automatic_scope_relaxation={relaxation}; "
        f"required_resolution={required_resolution}"
    )


__all__ = [
    "SMART_FAILURE_EXIT",
    "SMART_RESUMABLE_EXIT",
    "SMART_SUCCESS_EXIT",
    "AcquisitionAttemptCensus",
    "OperatorActionOrchestratorResult",
    "SmartCliDisposition",
    "SmartOrchestratorResult",
    "format_attempt_census",
    "format_temporal_disposition",
    "smart_cli_disposition",
]
