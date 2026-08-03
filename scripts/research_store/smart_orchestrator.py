"""PostgreSQL-backed planning persistence and state-aware smart-run recovery.

The legacy ``ResearchOrchestrator.run`` implementation assumes an in-memory
context beginning in ``created`` state.  This adapter preserves the existing
stage implementations while reconstructing their context from PostgreSQL and
``BLOB_ROOT`` after process restart.

No local path, manifest, Qdrant record, or Valkey entry is accepted as workflow
authority.  Search-response replay reads immutable payloads through the
content-addressed blob store referenced by PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from research_domain import load_model, serialize_model
from research_domain.models import ResearchSpec

from .domain import IngestRequest
from .orchestrator import OrchestratorResult, ResearchOrchestrator
from .run_service import RunStateError, StaleRunRevisionError
from .stages import ContextKeys, StageOutcome

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})
PLANNING_STATES = frozenset({"created", "planning"})
NETWORK_ENTRY_STATES = frozenset(
    {"created", "planning", "corpus_review", "coverage_review", "acquiring"}
)


class SmartResumeError(RuntimeError):
    """Persisted smart-run state is incomplete or cannot be resumed safely."""


@dataclass(frozen=True)
class PlanningBundle:
    """Authoritative planning records for one research run."""

    spec_row_id: UUID
    spec_revision: int
    spec: ResearchSpec
    budget_row_id: UUID
    budget: dict[str, Any]
    plan_row_id: UUID
    plan_revision: int
    plan: dict[str, Any]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise SmartResumeError("authoritative JSON payload is not an object")


def _latest_budget_row(uow: Any, run_id: UUID) -> dict[str, Any] | None:
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
        "snapshot": _as_dict(row[6]),
    }


def load_planning_bundle(run_service: Any, run_id: UUID) -> PlanningBundle | None:
    """Load the latest complete spec/budget/plan tuple for ``run_id``.

    A partially committed tuple returns ``None`` only when no planning record
    exists at all.  Once any component exists, missing or inconsistent members
    are treated as corruption and fail closed.
    """

    with run_service.uow_factory() as uow:
        spec_row = uow.runs.get_research_spec(run_id)
        budget_row = _latest_budget_row(uow, run_id)
        try:
            plan_row = uow.runs.get_search_plan(run_id)
        except (KeyError, ValueError):
            plan_row = None

    if spec_row is None and budget_row is None and plan_row is None:
        return None
    if spec_row is None:
        raise SmartResumeError("persisted smart run has budget/plan but no ResearchSpec")
    if budget_row is None:
        raise SmartResumeError("persisted smart run has no authoritative budget snapshot")
    if plan_row is None:
        raise SmartResumeError("persisted smart run has no authoritative search plan")

    spec_payload = _as_dict(spec_row["payload"])
    spec = load_model(spec_payload)
    if not isinstance(spec, ResearchSpec):
        raise SmartResumeError("persisted planning payload is not research-spec-v1")

    spec_row_id = UUID(str(spec_row["id"]))
    if UUID(str(budget_row["research_spec_id"])) != spec_row_id:
        raise SmartResumeError("budget snapshot references a different ResearchSpec")
    if UUID(str(plan_row["research_spec_id"])) != spec_row_id:
        raise SmartResumeError("search plan references a different ResearchSpec")

    budget = _as_dict(budget_row["snapshot"])
    plan = _as_dict(plan_row["payload"])
    if str(plan.get("research_spec_id", "")) != str(spec.research_spec_id):
        raise SmartResumeError("search-plan payload references a different domain spec ID")

    return PlanningBundle(
        spec_row_id=spec_row_id,
        spec_revision=int(spec_row["spec_revision"]),
        spec=spec,
        budget_row_id=UUID(str(budget_row["id"])),
        budget=budget,
        plan_row_id=UUID(str(plan_row["id"])),
        plan_revision=int(plan_row["revision"]),
        plan=plan,
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
    """Persist one immutable, idempotent planning tuple before acquisition."""

    spec_row_id = run_service.record_research_spec(
        run_id,
        spec=serialize_model(spec),
        revision=spec_revision,
        idempotency_key=f"smart:spec:{run_id}:r{spec_revision}",
        source="fsearch_smart",
    )
    with run_service.uow_factory() as uow:
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
    plan_row_id = run_service.record_search_plan(
        run_id,
        research_spec_id=spec_row_id,
        revision=int(plan.get("revision", spec_revision)),
        search_plan=plan,
        idempotency_key=f"smart:plan:{run_id}:r{int(plan.get('revision', spec_revision))}",
        source="fsearch_smart",
    )
    return PlanningBundle(
        spec_row_id=UUID(str(spec_row_id)),
        spec_revision=spec_revision,
        spec=spec,
        budget_row_id=UUID(str(budget_row_id)),
        budget=budget,
        plan_row_id=UUID(str(plan_row_id)),
        plan_revision=int(plan.get("revision", spec_revision)),
        plan=plan,
    )


def _coverage_context(orchestrator: ResearchOrchestrator, run_id: UUID) -> dict[str, Any]:
    context: dict[str, Any] = {}
    try:
        ledger = orchestrator.coverage_service.rebuild_projection(run_id)
    except Exception:  # No coverage records before corpus_review.
        return context
    context[ContextKeys.COVERAGE_LEDGER] = ledger
    status = getattr(getattr(ledger, "overall_status", None), "value", None)
    if status:
        context[ContextKeys.COVERAGE_STATUS] = status
        context[ContextKeys.OVERALL_STATUS] = status
    items = []
    candidate_targets: dict[str, list[str]] = {}
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
            candidate_targets.setdefault(str(candidate_id), []).append(item_id)
    context["coverage_items"] = items
    context["candidate_coverage_items"] = candidate_targets
    revision = int(getattr(ledger, "revision", 0) or 0)
    if revision:
        context["coverage_revision"] = revision
    return context


def _persisted_counts(orchestrator: ResearchOrchestrator, run_id: UUID) -> dict[str, int]:
    with orchestrator.run_service.uow_factory() as uow:
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT
                     count(*) FILTER (WHERE backend <> 'orchestrator'),
                     count(DISTINCT query_text) FILTER (WHERE backend <> 'orchestrator'),
                     (SELECT count(*) FROM extraction_attempts WHERE run_id=%s),
                     (SELECT count(*) FROM asset_snapshots s
                        JOIN extraction_attempts ea ON ea.id=s.extraction_attempt_id
                       WHERE ea.run_id=%s)
                   FROM search_responses WHERE run_id=%s""",
                (run_id, run_id, run_id),
            )
            row = cursor.fetchone()
    return {
        "responses": int(row[0] or 0),
        "waves": int(row[1] or 0),
        "attempts": int(row[2] or 0),
        "assets": int(row[3] or 0),
    }


def _restore_authorized_queries(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> list[dict[str, Any]]:
    try:
        proposals = orchestrator.strategy_service.list_proposals(run_id, limit=1000)
        decisions = orchestrator.strategy_service.list_decisions(
            run_id, outcome="accepted", limit=1000
        )
    except Exception:
        return []
    accepted = {
        str(getattr(item, "proposal_id", None) or item.get("proposal_id"))
        for item in decisions
    }
    restored = []
    for proposal in proposals:
        proposal_id = str(
            getattr(proposal, "proposal_id", None) or proposal.get("proposal_id")
        )
        queries = getattr(proposal, "proposed_queries", None)
        if queries is None and isinstance(proposal, dict):
            queries = proposal.get("proposed_queries")
        if proposal_id in accepted and queries:
            restored.append(
                {
                    "proposal_id": proposal_id,
                    "decision_type": "search",
                    "proposed_queries": list(queries),
                }
            )
    return restored


def _completed_candidate_ids(orchestrator: ResearchOrchestrator, run_id: UUID) -> set[str]:
    with orchestrator.run_service.uow_factory() as uow:
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT ea.candidate_id
                   FROM extraction_attempts ea
                   JOIN asset_snapshots s ON s.extraction_attempt_id=ea.id
                   WHERE ea.run_id=%s""",
                (run_id,),
            )
            return {str(row[0]) for row in cursor.fetchall()}


def _restore_raw_ingest_requests(
    orchestrator: ResearchOrchestrator,
    run_id: UUID,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reconstruct extraction inputs from PostgreSQL and immutable blobs only."""

    completed = _completed_candidate_ids(orchestrator, run_id)
    raw_requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    responses = orchestrator.run_service.list_search_responses(run_id)
    for response in responses:
        if response.get("backend") == "orchestrator" or response.get("status") == "failed":
            continue
        occurrences = orchestrator.run_service.record_response_candidates(
            run_id,
            UUID(str(response["id"])),
        )
        for occurrence in occurrences:
            candidate_id = str(occurrence.get("candidate_id") or "")
            if not candidate_id or candidate_id in seen or candidate_id in completed:
                continue
            seen.add(candidate_id)
            raw_item = occurrence.get("raw_item") or {}
            metadata = raw_item.get("metadata") or {}
            canonical_url = occurrence.get("canonical_url") or occurrence.get(
                "original_url"
            )
            request_metadata = {
                "candidate_id": candidate_id,
                "candidate_occurrence_id": str(occurrence.get("id")),
                "search_response_id": str(response["id"]),
                "firecrawl": {
                    "result_index": int(occurrence.get("rank") or 0),
                    "scrape_id": metadata.get("scrapeId"),
                    "source_url": metadata.get("sourceURL") or canonical_url,
                    "status_code": metadata.get("statusCode"),
                },
                "resume_replay": True,
            }
            markdown = raw_item.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                raw_requests.append(
                    {
                        "request": IngestRequest(
                            requested_url=canonical_url,
                            final_url=metadata.get("url")
                            or metadata.get("sourceURL")
                            or canonical_url,
                            content=markdown.encode("utf-8"),
                            normalized_content=markdown.encode("utf-8"),
                            mime_type="text/markdown",
                            title=occurrence.get("title"),
                            http_status=metadata.get("statusCode"),
                            firecrawl_version="cli-1.19.27",
                            crawl_options={
                                "operation": "search --scrape replay",
                                "formats": ["markdown"],
                            },
                            metadata=request_metadata,
                        ),
                        "metadata": request_metadata,
                    }
                )
            else:
                raw_requests.append(
                    {
                        "requested_url": canonical_url or "unknown:",
                        "error": "Firecrawl candidate has no scraped markdown",
                        "metadata": request_metadata,
                    }
                )
    context["raw_ingest_requests"] = raw_requests
    return raw_requests


def _restore_extracted_assets(
    orchestrator: ResearchOrchestrator, run_id: UUID
) -> list[dict[str, Any]]:
    with orchestrator.run_service.uow_factory() as uow:
        with uow.connection.cursor() as cursor:
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
            "chunk_ids": [str(item) for item in row[4]],
            "candidate_id": str(row[1]),
            "extraction_attempt_id": str(row[0]),
            "resume_replay": True,
        }
        for index, row in enumerate(rows)
    ]


def _latest_packet_revision(orchestrator: ResearchOrchestrator, run_id: UUID) -> int:
    with orchestrator.run_service.uow_factory() as uow:
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT packet_revision FROM evidence_packets
                   WHERE run_id=%s ORDER BY packet_revision DESC LIMIT 1""",
                (run_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise SmartResumeError("synthesizing run has no persisted EvidencePacket")
    return int(row[0])


class ResumableResearchOrchestrator(ResearchOrchestrator):
    """Run existing stages from the lifecycle state committed in PostgreSQL."""

    def _checkpoint(
        self, run_id: UUID, context: dict[str, Any], state: str
    ) -> OrchestratorResult | None:
        if context.get("_stop_after_state") != state:
            return None
        counts = _persisted_counts(self, run_id)
        return OrchestratorResult(
            run_id=run_id,
            final_state=state,
            outcome="checkpoint",
            coverage_revision=context.get("coverage_revision"),
            wave_count=counts["waves"],
            successful_urls=counts["assets"],
        )

    def _refresh(self, run_id: UUID) -> tuple[Any, str, int]:
        status = self.run_service.status(run_id=run_id)
        return status, status.state, status.lifecycle_revision

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
        ctx["spec"] = spec
        ctx["search_plan"] = search_plan
        ctx["execution_mode"] = self.orchestrator_config.execution_mode
        ctx["_max_adaptive_cycles"] = max_cycles
        ctx.setdefault(ContextKeys.WALL_CLOCK_START, time.monotonic())
        ctx.update({key: value for key, value in _coverage_context(self, run_id).items() if key not in ctx})
        ctx.setdefault(ContextKeys.AUTHORIZED_QUERIES, _restore_authorized_queries(self, run_id))
        counts = _persisted_counts(self, run_id)
        ctx.setdefault(ContextKeys.WAVE_COUNT, counts["waves"])
        ctx.setdefault(ContextKeys.EXTRACTION_ATTEMPTS, counts["attempts"])
        ctx.setdefault(ContextKeys.SUCCESSFUL_URLS, counts["assets"])

        status, state, revision = self._refresh(run_id)
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
                status, state, revision = self._refresh(run_id)
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
                    reason="resume from persisted spec, budget, and search plan",
                )
                status, state, revision = self._refresh(run_id)
                checkpoint = self._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint

            if state == "corpus_review":
                result = self._execute_stage(
                    "corpus_review", run_id, revision, coverage_revision, state, ctx
                )
                if result.error:
                    return self._failed_result(run_id, result.error)
                status, state, revision = self._refresh(run_id)
                ctx.update(_coverage_context(self, run_id))
                coverage_revision = int(ctx.get("coverage_revision") or 1)
                checkpoint = self._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint

            iterations = 0
            while state not in TERMINAL_STATES:
                iterations += 1
                if iterations > max(12, max_cycles * 6):
                    raise SmartResumeError("orchestrator resume loop exceeded safety bound")

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
                            reason="adaptive-cycle budget exhausted before another acquisition",
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
                        if int(ctx[ContextKeys.WAVE_COUNT]) >= max_cycles:
                            ctx["_budget_exhausted"] = True
                    status, state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "extracting":
                    raw_requests = list(ctx.get("raw_ingest_requests") or [])
                    if not raw_requests:
                        raw_requests = _restore_raw_ingest_requests(self, run_id, ctx)
                    if not raw_requests:
                        assets = _restore_extracted_assets(self, run_id)
                        next_state = "indexing" if assets else "coverage_review"
                        if assets:
                            ctx["extracted_assets"] = assets
                        self.run_service.transition(
                            run_id,
                            next_state,
                            expected_revision=revision,
                            idempotency_key=f"resume:extraction-empty:{run_id}:{next_state}",
                            actor_type="orchestrator",
                            actor_identifier="ResumableResearchOrchestrator",
                            triggering_event=f"run.{next_state}",
                            reason="resume found no unprocessed replayable candidates",
                        )
                    else:
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
                    status, state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "indexing":
                    ctx["extracted_assets"] = _restore_extracted_assets(self, run_id)
                    if not ctx["extracted_assets"]:
                        raise SmartResumeError(
                            "indexing run has no PostgreSQL-linked snapshots and chunks"
                        )
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
                    status, state, revision = self._refresh(run_id)
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
                    status, state, revision = self._refresh(run_id)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "retrieving":
                    self.run_service.transition(
                        run_id,
                        "coverage_review",
                        expected_revision=revision,
                        idempotency_key=f"resume:retrieval-review:{run_id}:{revision}",
                        actor_type="orchestrator",
                        actor_identifier="ResumableResearchOrchestrator",
                        triggering_event="run.coverage_review",
                        reason="resume retrieval checkpoint from authoritative corpus",
                    )
                    status, state, revision = self._refresh(run_id)
                    continue

                if state == "coverage_review":
                    ctx.update(_coverage_context(self, run_id))
                    coverage_revision = int(ctx.get("coverage_revision") or coverage_revision or 1)
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
                    status, state, revision = self._refresh(run_id)
                    ctx.update(_coverage_context(self, run_id))
                    coverage_revision = int(ctx.get("coverage_revision") or coverage_revision)
                    checkpoint = self._checkpoint(run_id, ctx, state)
                    if checkpoint:
                        return checkpoint
                    continue

                if state == "synthesizing":
                    ctx["evidence_packet_revision"] = _latest_packet_revision(self, run_id)
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
                    status, state, revision = self._refresh(run_id)
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
                    ctx["_terminal_reason"] = "resumed persisted validation checkpoint"
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
                    status, state, revision = self._refresh(run_id)
                    continue

                raise SmartResumeError(f"unsupported persisted run state: {state}")

            counts = _persisted_counts(self, run_id)
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
    "PlanningBundle",
    "ResumableResearchOrchestrator",
    "SmartResumeError",
    "TERMINAL_STATES",
    "load_planning_bundle",
    "persist_planning_bundle",
]
