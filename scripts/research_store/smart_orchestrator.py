"""Atomic smart-search planning and PostgreSQL-backed lifecycle resume.

This module adapts the existing staged orchestrator to process restarts. It
reconstructs stage inputs from PostgreSQL and immutable ``BLOB_ROOT`` payloads;
Qdrant and Valkey are never consulted as authorities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from research_domain import load_model, serialize_model
from research_domain.models import ResearchSpec, SearchPlan

from .domain import IngestRequest
from .orchestrator import OrchestratorResult, ResearchOrchestrator
from .stages import ContextKeys

if TYPE_CHECKING:
    from .resume_state_repository import PostgresResumeStateReader

logger = logging.getLogger(__name__)

NETWORK_ENTRY_STATES = frozenset(
    {"created", "planning", "corpus_review", "coverage_review", "acquiring"}
)
PLANNING_STATES = frozenset({"created", "planning"})
TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})


class SmartResumeError(RuntimeError):
    """Persisted smart-run state cannot be resumed without guessing."""


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
            plan_row = uow.runs.get_search_plan(run_id)
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
        plan_row_id = uow.runs.record_search_plan(
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


def _coverage_context(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> dict[str, Any]:
    ledger = orchestrator.coverage_service.rebuild_projection(run_id)
    status = getattr(getattr(ledger, "overall_status", None), "value", None)
    items: list[dict[str, Any]] = []
    targets: dict[str, list[str]] = {}
    for item in getattr(ledger, "items", ()):
        item_id = str(item.coverage_item_id)
        items.append(
            {
                "coverage_item_id": item_id,
                "item_type": getattr(item.item_type, "value", str(item.item_type)),
                "subject_id": str(item.subject_id),
                "remaining_gap": str(item.remaining_gap or ""),
            }
        )
        for candidate_id in getattr(item, "candidate_ids", ()):
            targets.setdefault(str(candidate_id), []).append(item_id)
    context: dict[str, Any] = {
        ContextKeys.COVERAGE_LEDGER: ledger,
        "coverage_items": items,
        "candidate_coverage_items": targets,
        "coverage_revision": int(getattr(ledger, "revision", 0) or 0),
    }
    if status:
        context[ContextKeys.COVERAGE_STATUS] = status
        context[ContextKeys.OVERALL_STATUS] = status
    return context


def _reader_for(orchestrator: ResearchOrchestrator) -> PostgresResumeStateReader:
    from .resume_state_repository import PostgresResumeStateReader

    return PostgresResumeStateReader(orchestrator.run_service.uow_factory)


def _counts(orchestrator: ResearchOrchestrator, run_id: UUID) -> dict[str, int]:
    counts = _reader_for(orchestrator).counts(run_id)
    return {"waves": counts.waves, "attempts": counts.attempts, "assets": counts.assets}


def _authorized_queries(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> list[dict[str, Any]]:
    return _reader_for(orchestrator).authorized_queries(run_id)


def _completed_candidates(orchestrator: ResearchOrchestrator, run_id: UUID) -> set[str]:
    return _reader_for(orchestrator).completed_candidates(run_id)


def _replay_extraction_inputs(
    orchestrator: ResearchOrchestrator,
    run_id: UUID,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Recreate unprocessed ingest requests from authoritative response blobs."""
    completed = _completed_candidates(orchestrator, run_id)
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for response in orchestrator.run_service.list_search_responses(run_id):
        if response.get("backend") == "orchestrator":
            continue
        if response.get("status") == "failed":
            continue
        occurrences = orchestrator.run_service.record_response_candidates(
            run_id, UUID(str(response["id"]))
        )
        for occurrence in occurrences:
            candidate_id = str(occurrence.get("candidate_id") or "")
            if not candidate_id or candidate_id in completed or candidate_id in seen:
                continue
            seen.add(candidate_id)
            raw_item = occurrence.get("raw_item") or {}
            firecrawl = raw_item.get("metadata") or {}
            url = occurrence.get("canonical_url") or occurrence.get("original_url")
            metadata = {
                "candidate_id": candidate_id,
                "candidate_occurrence_id": str(occurrence.get("id")),
                "search_response_id": str(response["id"]),
                "resume_replay": True,
                "firecrawl": {
                    "result_index": int(occurrence.get("rank") or 0),
                    "scrape_id": firecrawl.get("scrapeId"),
                    "source_url": firecrawl.get("sourceURL") or url,
                    "status_code": firecrawl.get("statusCode"),
                },
            }
            markdown = raw_item.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                requests.append(
                    {
                        "request": IngestRequest(
                            requested_url=url,
                            final_url=firecrawl.get("url")
                            or firecrawl.get("sourceURL")
                            or url,
                            content=markdown.encode(),
                            normalized_content=markdown.encode(),
                            mime_type="text/markdown",
                            title=occurrence.get("title"),
                            http_status=firecrawl.get("statusCode"),
                            firecrawl_version="cli-1.19.27",
                            crawl_options={
                                "operation": "search --scrape replay",
                                "formats": ["markdown"],
                            },
                            metadata=metadata,
                        ),
                        "metadata": metadata,
                    }
                )
            else:
                requests.append(
                    {
                        "requested_url": url or "unknown:",
                        "error": "Firecrawl candidate has no scraped markdown",
                        "metadata": metadata,
                    }
                )
    context["raw_ingest_requests"] = requests
    return requests


def _assets(orchestrator: ResearchOrchestrator, run_id: UUID) -> list[dict[str, Any]]:
    return _reader_for(orchestrator).assets(run_id)


def _packet_revision(orchestrator: ResearchOrchestrator, run_id: UUID) -> int:
    return _reader_for(orchestrator).packet_revision(run_id)


class ResumableResearchOrchestrator(ResearchOrchestrator):
    """Dispatch existing stage services from the persisted lifecycle state."""

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
        """Resume orchestration from persisted state.

        Thin facade delegating to ``orchestration.resume.run_resume``.
        """
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
