"""Atomic smart-search planning and PostgreSQL-backed lifecycle resume.

Smart-search planning persists through the explicit research and search-acquisition
repository roles. PostgreSQL reconstruction is provided through
``PostgresResumeStateReader`` and neutral application helpers; Qdrant and Valkey
are never consulted as authorities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain import load_model, serialize_model
from firecrawl_skill.research_domain.models import ResearchSpec, SearchPlan

from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .orchestration.resume_support import (
    NETWORK_ENTRY_STATES,
    PLANNING_STATES,
    TERMINAL_STATES,
    SmartResumeError,
    coverage_context,
    replay_extraction_inputs,
)
from .orchestrator import OrchestratorResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanningBundle:
    """One authoritative ResearchSpec/budget/search-plan tuple."""

    spec_row_id: UUID
    spec_revision: int
    spec: ResearchSpec
    budget_row_id: UUID
    budget: dict[str, Any]
    plan_row_id: UUID
    plan_revision: int
    plan: dict[str, Any]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise SmartResumeError(f"{label} is not a JSON object")


def _latest_budget(uow: Any, run_id: UUID) -> dict[str, Any] | None:
    row = uow.runs.get_latest_budget_snapshot(run_id)
    if row is None:
        return None
    return {
        "id": row["id"],
        "research_spec_id": row["research_spec_id"],
        "spec_revision": row["spec_revision"],
        "run_revision": row["run_revision"],
        "policy_version": row["policy_version"],
        "policy_config_sha256": row["policy_config_sha256"],
        "snapshot": _mapping(row["snapshot"], "budget snapshot"),
    }


def load_planning_bundle(run_service: Any, run_id: UUID) -> PlanningBundle | None:
    """Load a complete planning tuple or fail closed on partial persistence."""
    with run_service.uow_factory() as uow:
        spec_row = uow.runs.get_research_spec(run_id)
        budget_row = _latest_budget(uow, run_id)
        try:
            plan_row = uow.search_responses.get_search_plan(run_id)
        except (KeyError, ValueError):
            plan_row = None

    if spec_row is None and budget_row is None and plan_row is None:
        return None
    if spec_row is None:
        raise SmartResumeError("planning records contain no ResearchSpec")
    if budget_row is None:
        raise SmartResumeError("planning records contain no budget snapshot")
    if plan_row is None:
        raise SmartResumeError("planning records contain no search plan")

    spec = load_model(_mapping(spec_row["payload"], "ResearchSpec"))
    if not isinstance(spec, ResearchSpec):
        raise SmartResumeError("persisted spec is not research-spec-v1")
    plan_payload = _mapping(plan_row["payload"], "search plan")
    plan_model = load_model(plan_payload)
    if not isinstance(plan_model, SearchPlan):
        raise SmartResumeError("persisted plan is not search-plan-v1")

    spec_row_id = UUID(str(spec_row["id"]))
    if UUID(str(budget_row["research_spec_id"])) != spec_row_id:
        raise SmartResumeError("budget references another ResearchSpec row")
    if UUID(str(plan_row["research_spec_id"])) != spec_row_id:
        raise SmartResumeError("plan references another ResearchSpec row")
    if plan_model.research_spec_id != spec.research_spec_id:
        raise SmartResumeError("plan domain ID does not match ResearchSpec")

    return PlanningBundle(
        spec_row_id=spec_row_id,
        spec_revision=int(spec_row["spec_revision"]),
        spec=spec,
        budget_row_id=UUID(str(budget_row["id"])),
        budget=_mapping(budget_row["snapshot"], "budget snapshot"),
        plan_row_id=UUID(str(plan_row["id"])),
        plan_revision=int(plan_row["revision"]),
        plan=plan_payload,
    )


def persist_planning_bundle(
    run_service: Any,
    run_id: UUID,
    *,
    spec: ResearchSpec,
    budget: dict[str, Any],
    plan: dict[str, Any],
    spec_revision: int = 1,
    run_revision: int = 0,
) -> PlanningBundle:
    """Commit spec, budget, and plan in one PostgreSQL transaction."""
    plan_model = load_model(plan)
    if not isinstance(plan_model, SearchPlan):
        raise TypeError("provided planning payload is not search-plan-v1")
    spec_payload = serialize_model(spec)
    plan_revision = int(plan_model.revision)

    with run_service.uow_factory() as uow:
        spec_row_id = uow.runs.record_research_spec(
            run_id,
            spec_revision=spec_revision,
            schema_name="research_spec",
            schema_version=1,
            payload=spec_payload,
            idempotency_key=f"smart:spec:{run_id}:r{spec_revision}",
        )
        budget_row_id = uow.runs.record_budget_snapshot(
            run_id,
            spec_row_id,
            spec_revision,
            run_revision,
            str(budget["policy_version"]),
            str(budget["policy_config_sha256"]),
            budget,
            f"smart:budget:{run_id}:spec{spec_revision}:run{run_revision}",
        )
        plan_row_id = uow.search_responses.record_search_plan(
            run_id,
            spec_row_id,
            plan_revision,
            plan_model,
            f"smart:plan:{run_id}:r{plan_revision}",
        )
        uow.commit()

    return PlanningBundle(
        spec_row_id=UUID(str(spec_row_id)),
        spec_revision=spec_revision,
        spec=spec,
        budget_row_id=UUID(str(budget_row_id)),
        budget=budget,
        plan_row_id=UUID(str(plan_row_id)),
        plan_revision=plan_revision,
        plan=plan,
    )


def _coverage_context(orchestrator: Any, run_id: UUID) -> dict[str, Any]:
    """Delegate to the canonical coverage reconstruction helper."""
    return coverage_context(orchestrator, run_id)


def _reader_for(orchestrator: Any):
    from .resume_state_repository import PostgresResumeStateReader

    return PostgresResumeStateReader(orchestrator.run_service.uow_factory)


def _counts(orchestrator: Any, run_id: UUID) -> dict[str, int]:
    counts = _reader_for(orchestrator).counts(run_id)
    return {"waves": counts.waves, "attempts": counts.attempts, "assets": counts.assets}


def _authorized_queries(orchestrator: Any, run_id: UUID) -> list[dict[str, Any]]:
    return _reader_for(orchestrator).authorized_queries(run_id)


def _completed_candidates(orchestrator: Any, run_id: UUID) -> set[str]:
    return _reader_for(orchestrator).completed_candidates(run_id)


def _replay_extraction_inputs(
    orchestrator: Any,
    run_id: UUID,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Delegate to the canonical resume reconstruction helper."""
    return replay_extraction_inputs(
        orchestrator,
        run_id,
        context,
        completed_candidates=_completed_candidates(orchestrator, run_id),
    )


def _assets(orchestrator: Any, run_id: UUID) -> list[dict[str, Any]]:
    return _reader_for(orchestrator).assets(run_id)


def _packet_revision(orchestrator: Any, run_id: UUID) -> int:
    return _reader_for(orchestrator).packet_revision(run_id)


class ResumableResearchOrchestrator(CheckpointResearchOrchestrator):
    """Checkpoint-aware orchestrator for the canonical resume use case.

    Checkpoint semantics are explicit in the inheritance hierarchy. Production
    acquisition/extraction classes are supplied by the production composition
    boundary rather than a compatibility builder.
    """

    def _refresh(self, run_id: UUID) -> tuple[str, int]:
        status = self.run_service.status(run_id=run_id)
        return status.state, status.lifecycle_revision

    def _checkpoint(
        self, run_id: UUID, context: dict[str, Any], state: str
    ) -> OrchestratorResult | None:
        if context.get("_stop_after_state") != state:
            return None
        counts = _counts(self, run_id)
        return OrchestratorResult(
            run_id=run_id,
            final_state=state,
            outcome="checkpoint",
            coverage_revision=context.get("coverage_revision"),
            wave_count=counts["waves"],
            successful_urls=counts["assets"],
        )

    def run(
        self,
        run_id: UUID,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        max_adaptive_cycles: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """Resume orchestration from persisted state through the canonical use case."""
        from .orchestration.commands import RunResearchCommand
        from .orchestration.resume import run_resume

        command = RunResearchCommand(
            run_id=run_id,
            spec=spec,
            search_plan=search_plan,
            max_adaptive_cycles=max_adaptive_cycles,
            context=dict(context or {}),
        )
        return run_resume(self, command, state_port=_reader_for(self))


__all__ = [
    "NETWORK_ENTRY_STATES",
    "PLANNING_STATES",
    "TERMINAL_STATES",
    "PlanningBundle",
    "ResumableResearchOrchestrator",
    "SmartResumeError",
    "load_planning_bundle",
    "persist_planning_bundle",
]
