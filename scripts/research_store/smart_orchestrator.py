"""Atomic smart-search planning and PostgreSQL-backed lifecycle resume.

This module adapts the existing staged orchestrator to process restarts. It
reconstructs stage inputs from PostgreSQL and immutable ``BLOB_ROOT`` payloads;
Qdrant and Valkey are never consulted as authorities.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from research_domain import load_model, serialize_model
from research_domain.models import ResearchSpec, SearchPlan

from .domain import IngestRequest
from .orchestrator import OrchestratorResult, ResearchOrchestrator
from .run_service import RunStateError, StaleRunRevisionError
from .stages import ContextKeys

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
    with uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,research_spec_id,spec_revision,run_revision,
                      policy_version,policy_config_sha256,snapshot
               FROM research_budget_snapshots
               WHERE run_id=%s
               ORDER BY run_revision DESC,created_at DESC,id DESC
               LIMIT 1""",
            (run_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "research_spec_id": row[1],
        "spec_revision": int(row[2]),
        "run_revision": int(row[3]),
        "policy_version": str(row[4]),
        "policy_config_sha256": str(row[5]),
        "snapshot": _mapping(row[6], "budget snapshot"),
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


def _counts(orchestrator: ResearchOrchestrator, run_id: UUID) -> dict[str, int]:
    with (
        orchestrator.run_service.uow_factory() as uow,
        uow.connection.cursor() as cursor,
    ):
        cursor.execute(
            """SELECT
                 (SELECT count(*) FROM research_run_transitions
                   WHERE run_id=%s AND prior_state='acquiring'
                     AND next_state IN ('extracting','coverage_review')),
                 (SELECT count(*) FROM extraction_attempts WHERE run_id=%s),
                 (SELECT count(*) FROM asset_snapshots s
                    JOIN extraction_attempts ea ON ea.id=s.extraction_attempt_id
                   WHERE ea.run_id=%s)""",
            (run_id, run_id, run_id),
        )
        row = cursor.fetchone()
    return {
        "waves": int(row[0] or 0),
        "attempts": int(row[1] or 0),
        "assets": int(row[2] or 0),
    }


def _authorized_queries(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> list[dict[str, Any]]:
    with (
        orchestrator.run_service.uow_factory() as uow,
        uow.connection.cursor() as cursor,
    ):
        cursor.execute(
            """SELECT p.proposal_id,p.proposed_queries
               FROM strategy_revisions p
               WHERE p.run_id=%s AND p.row_type='proposal'
                 AND p.decision_type='search'
                 AND EXISTS (
                   SELECT 1 FROM strategy_revisions d
                    WHERE d.run_id=p.run_id AND d.row_type='decision'
                      AND d.proposal_id=p.proposal_id AND d.outcome='accepted'
                 )
               ORDER BY p.revision_order""",
            (run_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "proposal_id": str(proposal_id),
            "decision_type": "search",
            "proposed_queries": list(queries or []),
        }
        for proposal_id, queries in rows
        if queries
    ]


def _completed_candidates(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> set[str]:
    with (
        orchestrator.run_service.uow_factory() as uow,
        uow.connection.cursor() as cursor,
    ):
        cursor.execute(
            """SELECT DISTINCT ea.candidate_id
               FROM extraction_attempts ea
               JOIN asset_snapshots s ON s.extraction_attempt_id=ea.id
               WHERE ea.run_id=%s""",
            (run_id,),
        )
        return {str(row[0]) for row in cursor.fetchall()}


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


def _assets(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> list[dict[str, Any]]:
    with (
        orchestrator.run_service.uow_factory() as uow,
        uow.connection.cursor() as cursor,
    ):
        cursor.execute(
            """SELECT ea.id,ea.candidate_id,s.id,s.requested_url,
                      array_agg(ch.id ORDER BY ch.ordinal)
               FROM extraction_attempts ea
               JOIN asset_snapshots s ON s.extraction_attempt_id=ea.id
               JOIN documents d ON d.snapshot_id=s.id
               JOIN chunks ch ON ch.document_id=d.id
               WHERE ea.run_id=%s
               GROUP BY ea.id,ea.candidate_id,s.id,s.requested_url
               ORDER BY s.id""",
            (run_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "status": "complete",
            "ordinal": index,
            "requested_url": row[3],
            "snapshot_id": str(row[2]),
            "chunk_ids": [str(chunk_id) for chunk_id in row[4]],
            "candidate_id": str(row[1]),
            "extraction_attempt_id": str(row[0]),
            "resume_replay": True,
        }
        for index, row in enumerate(rows)
    ]


def _packet_revision(orchestrator: ResearchOrchestrator, run_id: UUID) -> int:
    with (
        orchestrator.run_service.uow_factory() as uow,
        uow.connection.cursor() as cursor,
    ):
        cursor.execute(
            """SELECT packet_revision FROM evidence_packets
               WHERE run_id=%s ORDER BY packet_revision DESC LIMIT 1""",
            (run_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise SmartResumeError("synthesizing run has no EvidencePacket")
    return int(row[0])


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
        max_cycles = max_adaptive_cycles or self.orchestrator_config.max_adaptive_cycles
        ctx = dict(context or {})
        ctx.update(
            {
                "spec": spec,
                "search_plan": search_plan,
                "execution_mode": self.orchestrator_config.execution_mode,
                "_max_adaptive_cycles": max_cycles,
            }
        )
        ctx.setdefault(ContextKeys.WALL_CLOCK_START, time.monotonic())
        state, revision = self._refresh(run_id)
        counts = _counts(self, run_id)
        ctx.setdefault(ContextKeys.WAVE_COUNT, counts["waves"])
        ctx.setdefault(ContextKeys.EXTRACTION_ATTEMPTS, counts["attempts"])
        ctx.setdefault(ContextKeys.SUCCESSFUL_URLS, counts["assets"])
        if state not in PLANNING_STATES and state not in TERMINAL_STATES:
            ctx.update(_coverage_context(self, run_id))
        ctx.setdefault(ContextKeys.AUTHORIZED_QUERIES, _authorized_queries(self, run_id))
        coverage_revision = int(ctx.get("coverage_revision") or 0) or None

        if state in TERMINAL_STATES:
            return OrchestratorResult(
                run_id=run_id,
                final_state=state,
                outcome="resumed",
                coverage_revision=coverage_revision,
                wave_count=counts["waves"],
                successful_urls=counts["assets"],
            )

        try:
            if state == "created":
                result = self._execute_stage(
                    "planning", run_id, revision, coverage_revision, state, ctx
                )
                if result.error:
                    return self._failed_result(run_id, result.error)
                state, revision = self._refresh(run_id)
                checkpoint = self._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
            elif state == "planning":
                self.run_service.transition(
                    run_id,
                    "corpus_review",
                    expected_revision=revision,
                    idempotency_key=f"resume:planning-complete:{run_id}",
                    actor_type="orchestrator",
                    actor_identifier="ResumableResearchOrchestrator",
                    triggering_event="run.corpus_review",
                    reason="resume from persisted planning tuple",
                )
                state, revision = self._refresh(run_id)

            if state == "corpus_review":
                result = self._execute_stage(
                    "corpus_review", run_id, revision, coverage_revision, state, ctx
                )
                if result.error:
                    return self._failed_result(run_id, result.error)
                state, revision = self._refresh(run_id)
                ctx.update(_coverage_context(self, run_id))
                coverage_revision = int(ctx.get("coverage_revision") or 1)
                checkpoint = self._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint

            iterations = 0
            while state not in TERMINAL_STATES:
                iterations += 1
                if iterations > max(12, max_cycles * 6):
                    raise SmartResumeError("resume loop exceeded its safety bound")

                if state == "acquiring":
                    if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                        ctx["_budget_exhausted"] = True
                        self.run_service.transition(
                            run_id,
                            "coverage_review",
                            expected_revision=revision,
                            idempotency_key=f"resume:budget-review:{run_id}:{revision}",
                            actor_type="orchestrator",
                            actor_identifier="ResumableResearchOrchestrator",
                            triggering_event="run.coverage_review",
                            reason="adaptive-cycle budget exhausted",
                        )
                    else:
                        result = self._execute_stage(
                            "acquisition",
                            run_id,
                            revision,
                            coverage_revision,
                            state,
                            ctx,
                        )
                        if result.error:
                            return self._failed_result(run_id, result.error)
                        ctx[ContextKeys.WAVE_COUNT] = int(
                            ctx.get(ContextKeys.WAVE_COUNT, 0)
                        ) + 1
                    state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "extracting":
                    inputs = list(ctx.get("raw_ingest_requests") or [])
                    if not inputs:
                        inputs = _replay_extraction_inputs(self, run_id, ctx)
                    if inputs:
                        result = self._execute_stage(
                            "extraction",
                            run_id,
                            revision,
                            coverage_revision,
                            state,
                            ctx,
                        )
                        if result.error:
                            return self._failed_result(run_id, result.error)
                    else:
                        restored = _assets(self, run_id)
                        next_state = "indexing" if restored else "coverage_review"
                        ctx["extracted_assets"] = restored
                        self.run_service.transition(
                            run_id,
                            next_state,
                            expected_revision=revision,
                            idempotency_key=f"resume:extraction:{run_id}:{next_state}",
                            actor_type="orchestrator",
                            actor_identifier="ResumableResearchOrchestrator",
                            triggering_event=f"run.{next_state}",
                            reason="resume found no unprocessed candidates",
                        )
                    state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "indexing":
                    ctx["extracted_assets"] = _assets(self, run_id)
                    if not ctx["extracted_assets"]:
                        raise SmartResumeError("indexing state has no persisted chunks")
                    result = self._execute_stage(
                        "indexing",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return self._failed_result(run_id, result.error)
                    state, revision = self._refresh(run_id)
                    result = self._execute_stage(
                        "evidence_preparation",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return self._failed_result(run_id, result.error)
                    state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "retrieving":
                    self.run_service.transition(
                        run_id,
                        "coverage_review",
                        expected_revision=revision,
                        idempotency_key=f"resume:retrieval:{run_id}:{revision}",
                        actor_type="orchestrator",
                        actor_identifier="ResumableResearchOrchestrator",
                        triggering_event="run.coverage_review",
                        reason="resume retrieval from authoritative corpus",
                    )
                    state, revision = self._refresh(run_id)
                    continue

                if state == "coverage_review":
                    ctx.update(_coverage_context(self, run_id))
                    coverage_revision = int(ctx.get("coverage_revision") or 1)
                    if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                        ctx["_budget_exhausted"] = True
                    result = self._execute_stage(
                        "coverage_review",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return self._failed_result(run_id, result.error)
                    state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "synthesizing":
                    ctx["evidence_packet_revision"] = _packet_revision(self, run_id)
                    result = self._execute_stage(
                        "synthesis",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return self._failed_result(run_id, result.error)
                    state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "validating":
                    ctx.update(_coverage_context(self, run_id))
                    ctx["_terminal_outcome"] = (
                        "completed"
                        if ctx.get(ContextKeys.OVERALL_STATUS) == "sufficient"
                        else "partial"
                    )
                    ctx["_terminal_reason"] = "resumed validation checkpoint"
                    result = self._execute_stage(
                        "terminal",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return self._failed_result(run_id, result.error)
                    state, revision = self._refresh(run_id)
                    continue

                raise SmartResumeError(f"unsupported persisted state: {state}")

            counts = _counts(self, run_id)
            return OrchestratorResult(
                run_id=run_id,
                final_state=state,
                outcome=state,
                coverage_revision=coverage_revision,
                wave_count=counts["waves"],
                successful_urls=counts["assets"],
            )
        except (RunStateError, StaleRunRevisionError, SmartResumeError) as exc:
            logger.error("smart-run resume failed: %s", exc)
            return self._failed_result(run_id, str(exc))


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
