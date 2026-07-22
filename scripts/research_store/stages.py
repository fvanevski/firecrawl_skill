"""Stage protocol and stage-specific dataclasses for the coverage-led orchestrator.

This module defines:

* The ``StageResult`` dataclass used by every stage handler.
* The ``StageHandler`` Protocol that every stage must implement.
* Stage-specific result types that carry structured output for downstream
  stages (search plans, coverage summaries, strategy decisions, etc.).

The orchestrator composes these stages into an explicit pipeline:

    planning -> corpus_review -> acquisition -> extraction -> indexing ->
    coverage_review -> next_action -> (retrieval | synthesis | stop)

Every stage is responsible for:

1. Validating its inputs.
2. Performing its work (or delegating to services).
3. Recording invocations and events via the unit of work.
4. Returning a ``StageResult`` that the orchestrator uses to decide the
   next stage or a terminal outcome.

No stage may execute a semantic proposal directly.  Strategy proposals
must be persisted and authorized through ``StrategyRevisionService``
before any adaptive action executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from uuid import UUID


# ---------------------------------------------------------------------------
# Stage result types
# ---------------------------------------------------------------------------


class StageOutcome(str, Enum):
    """Outcome reported by a stage to the orchestrator."""

    CONTINUE = "continue"  # proceed to next stage
    REPEAT = "repeat"  # repeat the same stage (e.g. retry)
    TERMINAL = "terminal"  # run is complete (stop)
    DEGRADED = "degraded"  # stage partially succeeded


@dataclass(frozen=True)
class StageResult:
    """Compact result returned by every stage handler.

    Attributes:
        stage: The stage name that produced this result.
        outcome: Whether to continue, repeat, or stop.
        state: The research run state this stage transitioned to (if any).
        summary: Human-readable summary of what the stage did.
        details: Structured data for downstream stages.
        events: Event types emitted by this stage (for observability).
        warnings: Non-fatal warnings (degraded components, etc.).
        error: Error message if the stage failed.
    """

    stage: str
    outcome: StageOutcome
    summary: str = ""
    details: dict[str, Any] | None = None
    events: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def ok(cls, stage: str, summary: str, **kwargs: Any) -> "StageResult":
        return cls(
            stage=stage,
            outcome=StageOutcome.CONTINUE,
            summary=summary,
            events=(f"stage.{stage}.completed",),
            **kwargs,
        )

    @classmethod
    def terminal(cls, stage: str, summary: str, **kwargs: Any) -> "StageResult":
        return cls(
            stage=stage,
            outcome=StageOutcome.TERMINAL,
            summary=summary,
            events=(f"stage.{stage}.terminal",),
            **kwargs,
        )

    @classmethod
    def degraded(cls, stage: str, summary: str, **kwargs: Any) -> "StageResult":
        return cls(
            stage=stage,
            outcome=StageOutcome.DEGRADED,
            summary=summary,
            warnings=(f"stage.{stage}.degraded",),
            **kwargs,
        )

    @classmethod
    def failed(cls, stage: str, error: str, **kwargs: Any) -> "StageResult":
        return cls(
            stage=stage,
            outcome=StageOutcome.TERMINAL,
            error=error,
            events=(f"stage.{stage}.failed",),
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Stage handler protocol
# ---------------------------------------------------------------------------


class StageHandler(Protocol):
    """Every stage must implement this protocol.

    Args:
        run_id: The research run being orchestrated.
        run_revision: Current lifecycle revision of the run.
        coverage_revision: Current coverage revision (may be None before planning).
        run_state: Current state of the run.
        context: Arbitrary context dict shared across stages.

    Returns:
        A StageResult describing what happened and what to do next.
    """

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult: ...


# ---------------------------------------------------------------------------
# Stage-specific context keys
# ---------------------------------------------------------------------------


class ContextKeys:
    """Stable keys used to exchange data between stages."""

    # Planning stage output
    SPEC_ID = "spec_id"
    SPEC_REVISION = "spec_revision"
    SEARCH_PLAN_ID = "search_plan_id"
    SEARCH_PLAN_REVISION = "search_plan_revision"
    QUERY_COUNT = "query_count"

    # Acquisition stage output
    SEARCH_RESPONSE_IDS = "search_response_ids"
    CANDIDATE_COUNT = "candidate_count"
    SUCCESSFUL_URLS = "successful_urls"
    ACQUIRED_CANDIDATE_IDS = "acquired_candidate_ids"

    # Extraction stage output
    EXTRACTION_ATTEMPTS = "extraction_attempts"
    EXTRACTION_SUCCESS_COUNT = "extraction_success_count"

    # Indexing stage output
    INDEX_BUILD_ID = "index_build_id"
    INDEX_FINGERPRINT = "index_fingerprint"

    # Coverage stage output
    COVERAGE_LEDGER = "coverage_ledger"
    COVERAGE_STATUS = "coverage_status"
    COVERAGE_ITEMS = "coverage_items"
    OVERALL_STATUS = "overall_status"
    STATUS_COUNTS = "status_counts"

    # Strategy stage output
    STRATEGY_PROPOSAL_ID = "strategy_proposal_id"
    STRATEGY_DECISION_ID = "strategy_decision_id"
    STRATEGY_DECISION = "strategy_decision"
    NEXT_ACTION = "next_action"
    AUTHORIZED_QUERIES = "authorized_queries"  # Queries from authorized proposals

    # Retrieval stage output
    RETRIEVAL_PASSAGES = "retrieval_passages"
    RETRIEVAL_COUNT = "retrieval_count"

    # Synthesis stage output
    SYNTHESIS_ARTIFACT_ID = "synthesis_artifact_id"
    REPORT_ID = "report_id"

    # Diagnostics
    WALL_CLOCK_START = "wall_clock_start"
    DURATION_MS = "duration_ms"
    WAVE_COUNT = "wave_count"


# ---------------------------------------------------------------------------
# Coverage outcome helpers
# ---------------------------------------------------------------------------


# Strategy decision types — distinct from run state names.
# These are the values persisted on StrategyRevisionProposal.decision_type.
STRATEGY_DECISION_SYNTHESIZE = "synthesize"
STRATEGY_DECISION_SEARCH = "search"
STRATEGY_DECISION_PARTIAL = "partial"
STRATEGY_DECISION_FAIL = "fail"

# Mapping from strategy decision type to run state name.
_DECISION_TO_STATE: dict[str, str] = {
    STRATEGY_DECISION_SYNTHESIZE: "synthesizing",
    STRATEGY_DECISION_SEARCH: "acquiring",
    STRATEGY_DECISION_PARTIAL: "partial",
    STRATEGY_DECISION_FAIL: "failed",
}


def _coverage_decision(
    overall_status: str,
    budget_exhausted: bool = False,
    no_progress: bool = False,
) -> tuple[str, str]:
    """Determine the next action based on coverage status.

    Returns:
        (decision_type, reason) — one of:
        (``STRATEGY_DECISION_SYNTHESIZE``, ...) |
        (``STRATEGY_DECISION_SEARCH``, ...) |
        (``STRATEGY_DECISION_PARTIAL``, ...) |
        (``STRATEGY_DECISION_FAIL``, ...)
    """
    if no_progress:
        return (STRATEGY_DECISION_FAIL, "no_progress")

    if budget_exhausted:
        if overall_status == "sufficient":
            return (STRATEGY_DECISION_SYNTHESIZE, "budget_exhausted_sufficient")
        return (STRATEGY_DECISION_PARTIAL, "budget_exhausted_insufficient")

    if overall_status == "sufficient":
        return (STRATEGY_DECISION_SYNTHESIZE, "coverage_sufficient")

    if overall_status == "blocked":
        return (STRATEGY_DECISION_FAIL, "coverage_blocked")

    # partial or insufficient — continue acquiring
    if overall_status in ("partial", "insufficient", "unassessed"):
        return (STRATEGY_DECISION_SEARCH, f"coverage_{overall_status}")

    return (STRATEGY_DECISION_PARTIAL, f"unknown_coverage_{overall_status}")


def decision_to_state(decision_type: str) -> str:
    """Map a strategy decision type to the corresponding run state name."""
    return _DECISION_TO_STATE.get(
        decision_type, _DECISION_TO_STATE[STRATEGY_DECISION_PARTIAL]
    )
