"""Durable smart-search attempt census attached to orchestration results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .orchestrator import OrchestratorResult


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
class SmartOrchestratorResult(OrchestratorResult):
    """Orchestrator result plus PostgreSQL-derived extraction-attempt census."""

    attempted_urls: int = 0
    successful_attempts: int = 0
    unsuccessful_urls: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    unsuccessful_attempts: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_result(
        cls,
        result: OrchestratorResult,
        census: AcquisitionAttemptCensus,
    ) -> SmartOrchestratorResult:
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
            }
        )
        return payload


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
    "SmartOrchestratorResult",
    "format_attempt_census",
]
