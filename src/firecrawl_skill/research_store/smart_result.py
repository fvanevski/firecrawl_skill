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


def smart_cli_disposition(result: OrchestratorResult) -> SmartCliDisposition:
    """Map one canonical workflow result to process status and next action.

    ``partial`` remains a successful *terminal* workflow result under the
    existing terminal contract.  Recoverable work is explicitly non-success
    at the process boundary even when no error occurred.
    """

    outcome = str(result.outcome)
    state = str(result.final_state)
    if outcome in {"checkpoint", "resumable", "operator_action_required"}:
        next_action = (
            "resolve_candidate_budget_override_then_resume_same_run"
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


__all__ = [
    "AcquisitionAttemptCensus",
    "OperatorActionOrchestratorResult",
    "SMART_FAILURE_EXIT",
    "SMART_RESUMABLE_EXIT",
    "SMART_SUCCESS_EXIT",
    "SmartCliDisposition",
    "SmartOrchestratorResult",
    "format_attempt_census",
    "smart_cli_disposition",
]
