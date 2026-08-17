"""Coverage-led research orchestrator.

This module replaces the monolithic ``fsearch_smart`` loop with an explicit,
staged orchestrator that:

1. Transitions the research run through explicit states via ``ResearchRunService``.
2. Creates coverage items from the ``ResearchSpec`` before acquisition.
3. Evaluates coverage after each meaningful wave.
4. Proposes and authorizes adaptive actions through ``StrategyRevisionService``.
5. Persists every invocation and state transition.
6. Uses successful-page count only as diagnostic metadata.
7. Permits sufficient runs to stop below page targets.
8. Prevents insufficient runs from completing because enough pages succeeded.
9. Resumes after process restart by detecting existing run state.

The orchestrator is the single entry point for the coverage-led workflow.
All state transitions flow through ``ResearchRunService`` — no second state
machine exists.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from budget_policy import DEFAULT_POLICY
from research_domain import load_model

from .acquisition_service import AcquisitionService
from .config import StoreConfig
from .coverage_service import CoverageService
from .run_service import (
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
)
from .stages import (
    STRATEGY_DECISION_FAIL,
    STRATEGY_DECISION_PARTIAL,
    STRATEGY_DECISION_SEARCH,
    STRATEGY_DECISION_SYNTHESIZE,
    ContextKeys,
    StageHandler,
    StageOutcome,
    StageResult,
    _coverage_decision,
    decision_to_state,
)
from .strategy_service import StrategyRevisionService
from .terminal_decision import (
    TerminalDecision,
    TerminalDecisionConfig,
    TerminalDecisionOutcome,
    TerminalDecisionPolicy,
    TerminalDecisionPolicyError,
)
from .terminal_decision_service import TerminalDecisionService

logger = logging.getLogger(__name__)


def _extraction_failure_class(error: object) -> str:
    """Map durable ingest errors onto the registered extraction taxonomy."""
    message = str(error or "").lower()
    if "no scraped markdown" in message or "empty" in message:
        return "empty_content"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "network" in message or "connection" in message:
        return "network"
    if "http" in message:
        return "http_error"
    if "parser" in message:
        return "parser"
    if "malformed" in message or "decode" in message:
        return "malformed"
    return "internal"


def _minimum_authoritative_source_target(spec: dict[str, Any]) -> int:
    """Return a substantive, policy-bounded source target for one run."""
    requirements = list(spec.get("required_source_classes", ())) + list(
        spec.get("corroboration_requirements", ())
    )
    declared = [
        int(item.get("minimum_count", item.get("minimum_independent_sources", 0)))
        for item in requirements
    ]
    return max(3, max(declared, default=0))


# ---------------------------------------------------------------------------
# Orchestrator configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for the coverage-led orchestrator.

    Attributes:
        execution_mode: The execution mode for this run.
        budget_policy_version: Version string for the budget policy.
        max_adaptive_cycles: Maximum number of coverage-review cycles.
        resume_on_conflict: If True, resume an existing run instead of failing.
        resource_governor: Optional ResourceGovernor for bounded concurrent
            generative calls.  When provided, synthesis LLM calls are gated
            through the governor.
    """

    execution_mode: str = "autonomous_local"
    budget_policy_version: str = "budget-policy-v1"
    max_adaptive_cycles: int = 10
    resume_on_conflict: bool = True
    resource_governor: Any = None
    host_artifact_supplier: Any = None


# ---------------------------------------------------------------------------
# Orchestrator result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorResult:
    """Final result of an orchestrator invocation.

    Attributes:
        run_id: The research run that was orchestrated.
        final_state: The terminal state the run ended in.
        outcome: One of "completed", "partial", "failed", "resumed".
        coverage_revision: Final coverage revision.
        wave_count: Number of acquisition waves executed.
        successful_urls: Diagnostic count of successful scrapes.
        strategy_proposals: Number of strategy proposals created.
        strategy_decisions: Number of strategy decisions authorized.
        error: Error message if the run failed.
    """

    run_id: UUID
    final_state: str
    outcome: str
    coverage_revision: int | None = None
    wave_count: int = 0
    successful_urls: int = 0
    strategy_proposals: int = 0
    strategy_decisions: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "final_state": self.final_state,
            "outcome": self.outcome,
            "coverage_revision": self.coverage_revision,
            "wave_count": self.wave_count,
            "successful_urls": self.successful_urls,
            "strategy_proposals": self.strategy_proposals,
            "strategy_decisions": self.strategy_decisions,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


class PlanningStage:
    """Plan the research: create spec and search plan.

    Transitions: created -> planning -> corpus_review
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("created", "planning"):
            return StageResult.failed(
                "planning",
                f"planning stage requires created/planning state, got {run_state}",
            )

        # Transition to planning
        try:
            self.run_service.transition(
                run_id,
                "planning",
                expected_revision=run_revision,
                idempotency_key=f"stage:planning:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="PlanningStage",
                triggering_event="run.planning",
                reason="coverage-led planning",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("planning", str(exc))

        # The spec and search plan are expected to be provided by the caller
        # through context (set during --research-spec or frun start).
        spec = context.get("spec")
        search_plan = context.get("search_plan")

        if spec is None:
            return StageResult.failed(
                "planning",
                "ResearchSpec not provided in context; "
                "use --research-spec or frun start to supply one",
            )

        # Record spec if not already recorded
        spec_id = context.get(ContextKeys.SPEC_ID)
        spec_revision = context.get(ContextKeys.SPEC_REVISION, 1)
        if spec_id is None:
            spec_revision = 1
            spec_uuid = UUID(str(spec.get("research_spec_id", uuid4())))
            spec["research_spec_id"] = str(spec_uuid)
            if hasattr(self.run_service, "record_research_spec"):
                self.run_service.record_research_spec(
                    run_id,
                    spec=spec,
                    revision=spec_revision,
                )
            plan_payload = search_plan or {
                "schema_version": "search-plan-v1",
                "research_spec_id": str(spec_uuid),
                "revision": spec_revision,
                "queries": [],
            }
            spec_id = self.run_service.record_search_plan(
                run_id,
                research_spec_id=spec_uuid,
                revision=spec_revision,
                search_plan=plan_payload,
                idempotency_key=f"spec:{run_id}:{spec_revision}",
            )
            context[ContextKeys.SPEC_ID] = spec_id
            context[ContextKeys.SPEC_REVISION] = spec_revision

        # Transition to corpus_review
        try:
            self.run_service.transition(
                run_id,
                "corpus_review",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:planning_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="PlanningStage",
                triggering_event="run.corpus_review",
                reason="planning complete, ready for corpus review",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("planning", str(exc))

        return StageResult.ok(
            "planning",
            "spec and search plan recorded, transitioned to corpus_review",
            details={
                ContextKeys.SPEC_ID: str(spec_id),
                ContextKeys.SPEC_REVISION: spec_revision,
            },
        )


class CorpusReviewStage:
    """Create coverage items from the ResearchSpec.

    Transitions: corpus_review -> acquiring
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "corpus_review":
            return StageResult.failed(
                "corpus_review",
                f"corpus_review stage requires corpus_review state, got {run_state}",
            )

        spec = context.get("spec")
        if spec is None:
            return StageResult.failed(
                "corpus_review", "ResearchSpec not available for coverage creation"
            )

        execution_mode = context.get("execution_mode", "autonomous_local")

        # Create coverage items from the spec
        try:
            items = self.coverage_service.create_items_from_spec(
                run_id,
                spec,
                execution_mode=execution_mode,
                idempotency_key=f"coverage:items:{run_id}",
                source_event_id=None,
                source_invocation_id=None,
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "corpus_review", f"coverage creation failed: {exc}"
            )

        context["coverage_items"] = [
            {
                "coverage_item_id": str(item.coverage_item_id),
                "item_type": item.item_type.value,
                "subject_id": item.subject_id,
                "remaining_gap": item.remaining_gap,
            }
            for item in items
        ]

        # Create initial snapshot
        try:
            self.coverage_service.create_snapshot(
                run_id,
                ledger={
                    "schema_version": "coverage-ledger-v1",
                    "run_id": str(run_id),
                    "revision": 1,
                    "items": [
                        {
                            "coverage_item_id": str(item.coverage_item_id),
                            "item_type": item.item_type.value,
                            "subject_id": item.subject_id,
                            "status": item.status.value,
                            "candidate_ids": [],
                            "snapshot_ids": [],
                            "passage_ids": [],
                            "independent_source_count": 0,
                            "required_independent_source_count": 0,
                            "authority_classes_present": [],
                            "freshness_status": item.freshness_status.value,
                            "remaining_gap": item.remaining_gap,
                            "confidence": item.confidence,
                        }
                        for item in items
                    ],
                    "overall_status": "unassessed",
                },
                coverage_revision=1,
                idempotency_key=f"snapshot:initial:{run_id}",
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "corpus_review", f"snapshot creation failed: {exc}"
            )

        # Update run's current_coverage_revision
        try:
            self.run_service.transition(
                run_id,
                "acquiring",
                expected_revision=run_revision,
                idempotency_key=f"stage:corpus_review_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="CorpusReviewStage",
                triggering_event="run.acquiring",
                reason=f"coverage items created ({len(items)} items, revision 1)",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("corpus_review", str(exc))

        return StageResult.ok(
            "corpus_review",
            f"created {len(items)} coverage items, transitioned to acquiring",
            details={
                ContextKeys.COVERAGE_STATUS: "unassessed",
                ContextKeys.OVERALL_STATUS: "unassessed",
            },
        )


class AcquisitionStage:
    """Execute search queries and persist candidates.

    Transitions: acquiring -> (extracting | coverage_review)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        acquisition_service: AcquisitionService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.acquisition_service = acquisition_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("acquiring", "coverage_review"):
            return StageResult.failed(
                "acquisition",
                f"acquisition stage requires acquiring/coverage_review state, got {run_state}",
            )

        search_plan = context.get("search_plan")
        if search_plan is None:
            return StageResult.failed(
                "acquisition", "Search plan not available for acquisition"
            )

        queries = search_plan.get("queries", [])

        try:
            budget = DEFAULT_POLICY.evaluate(
                load_model(context["spec"]),
                spec_revision=int(context.get(ContextKeys.SPEC_REVISION, 1)),
                run_revision=run_revision,
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "acquisition", f"could not evaluate acquisition budget: {exc}"
            )
        caps = budget.effective_caps
        context["effective_resource_caps"] = caps.to_dict()
        source_target = min(
            caps.max_successful_extractions,
            _minimum_authoritative_source_target(context["spec"]),
        )
        attempt_target = min(caps.max_extraction_attempts, source_target + 3)

        # Pass Strategy Queries to AcquisitionStage: In cycle 2+, extract
        # authorized queries from strategy proposals and merge with the
        # original search plan. This allows the orchestrator to execute
        # adaptive queries proposed during coverage_review.
        authorized_proposals = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
        if authorized_proposals:
            # Merge authorized queries with the original search plan,
            # avoiding duplicates by query text.
            existing_texts = {q.get("query", "") for q in queries}
            for proposal in authorized_proposals:
                for q in proposal.get("proposed_queries", []):
                    query_text = q.get("query", "")
                    if query_text and query_text not in existing_texts:
                        queries.append(q)
                        existing_texts.add(query_text)

        if not queries:
            return StageResult.failed(
                "acquisition", "Search plan has no queries to execute"
            )

        # Execute each query through the acquisition service
        response_ids = []
        candidate_count = 0
        successful_urls = 0
        candidate_ids = []  # Collect actual candidate IDs for coverage events
        raw_ingest_requests: list[dict[str, Any]] = []
        candidate_targets: dict[str, list[str]] = context.setdefault(
            "candidate_coverage_items", {}
        )
        scheduled_candidates: set[str] = context.setdefault(
            "scheduled_candidate_ids", set()
        )
        executed_queries: set[str] = context.setdefault("executed_query_texts", set())
        extraction_attempt_count = int(context.get("extraction_attempt_count", 0))
        successful_extraction_count = int(context.get("successful_extraction_count", 0))
        coverage_by_subject = {
            str(item["subject_id"]): str(item["coverage_item_id"])
            for item in context.get("coverage_items", [])
        }

        for query in queries:
            query_text = query.get("query", "")
            if not query_text or query_text in executed_queries:
                continue
            if len(executed_queries) >= caps.max_search_branches:
                break

            try:
                result = self.acquisition_service.execute_search(
                    run_id,
                    query_text,
                    backend=(
                        "firecrawl_scrape"
                        if query.get("facet") == "benchmark_source"
                        else "firecrawl"
                    ),
                    idempotency_key=f"acquire:{run_id}:{query_text}",
                    limit=caps.results_per_branch,
                )
                executed_queries.add(query_text)
                response_ids.append(str(result.search_response_id))
                candidate_count += result.candidate_count
                query_targets = [
                    coverage_by_subject[target]
                    for target in (
                        list(query.get("target_question_ids", []))
                        + list(query.get("target_claim_ids", []))
                    )
                    if target in coverage_by_subject
                ]
                if query.get("intended_source_classes"):
                    query_targets.extend(
                        str(item["coverage_item_id"])
                        for item in context.get("coverage_items", [])
                        if item.get("item_type") == "source_requirement"
                    )
                if query.get("freshness_requirement"):
                    query_targets.extend(
                        str(item["coverage_item_id"])
                        for item in context.get("coverage_items", [])
                        if item.get("item_type") == "freshness_requirement"
                    )
                query_targets = list(dict.fromkeys(query_targets))
                for cand in result.candidates:
                    cid = cand.get("candidate_id") or cand.get("id")
                    if cid:
                        cid_str = str(cid)
                        candidate_ids.append(cid_str)
                        existing_targets = candidate_targets.setdefault(cid_str, [])
                        for target in query_targets:
                            if target not in existing_targets:
                                existing_targets.append(target)

                        if cid_str in scheduled_candidates:
                            continue
                        if extraction_attempt_count >= attempt_target:
                            continue
                        scheduled_candidates.add(cid_str)

                    raw_item = cand.get("raw_item") or {}
                    markdown = raw_item.get("markdown")
                    metadata = raw_item.get("metadata") or {}
                    request_metadata = {
                        "candidate_id": str(cid) if cid else None,
                        "candidate_occurrence_id": str(cand.get("id")),
                        "search_response_id": str(result.search_response_id),
                        "firecrawl": {
                            "result_index": len(raw_ingest_requests),
                            "scrape_id": metadata.get("scrapeId"),
                            "source_url": metadata.get("sourceURL")
                            or cand.get("canonical_url"),
                            "status_code": metadata.get("statusCode"),
                        },
                    }
                    if isinstance(markdown, str) and markdown.strip() and cid:
                        if successful_extraction_count >= source_target:
                            continue
                        from .domain import IngestRequest

                        raw_ingest_requests.append(
                            {
                                "request": IngestRequest(
                                    requested_url=cand.get("canonical_url")
                                    or cand.get("original_url"),
                                    final_url=metadata.get("url")
                                    or metadata.get("sourceURL")
                                    or cand.get("canonical_url"),
                                    content=markdown.encode("utf-8"),
                                    normalized_content=markdown.encode("utf-8"),
                                    mime_type="text/markdown",
                                    title=cand.get("title"),
                                    http_status=metadata.get("statusCode"),
                                    firecrawl_version="cli-1.19.27",
                                    crawl_options={
                                        "operation": "search --scrape",
                                        "formats": ["markdown"],
                                    },
                                    metadata=request_metadata,
                                ),
                                "metadata": request_metadata,
                            }
                        )
                        successful_urls += 1
                        successful_extraction_count += 1
                    else:
                        raw_ingest_requests.append(
                            {
                                "requested_url": cand.get("canonical_url")
                                or cand.get("original_url")
                                or "unknown:",
                                "error": "Firecrawl candidate has no scraped markdown",
                                "metadata": request_metadata,
                            }
                        )
                    extraction_attempt_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("acquisition query failed: %s — %s", query_text, exc)

        # Apply candidate_identified events to coverage
        if coverage_revision is not None:
            try:
                # Get wave count from context for cycle-scoped idempotency keys
                wave_count = context.get(ContextKeys.WAVE_COUNT, 0)

                # Apply one event per candidate to track individual discoveries
                # Use cycle-scoped idempotency keys and actual candidate IDs
                for i, cand_id in enumerate(candidate_ids):
                    for item_id in candidate_targets.get(cand_id, []):
                        self.coverage_service.apply_candidate_identified(
                            run_id,
                            UUID(item_id),
                            candidate_id=UUID(cand_id),
                            idempotency_key=(
                                f"acquire:cand:{run_id}:w{wave_count}:{i}:{item_id}"
                            ),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("coverage update after acquisition failed: %s", exc)

        # Update context with acquisition results
        context[ContextKeys.SEARCH_RESPONSE_IDS] = response_ids
        context[ContextKeys.CANDIDATE_COUNT] = candidate_count
        context[ContextKeys.SUCCESSFUL_URLS] = successful_urls
        context["raw_ingest_requests"] = raw_ingest_requests
        context["extraction_attempt_count"] = extraction_attempt_count
        context["successful_extraction_count"] = successful_extraction_count

        # Transition to extraction or coverage_review
        if raw_ingest_requests:
            try:
                self.run_service.transition(
                    run_id,
                    "extracting",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:acquisition_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="AcquisitionStage",
                    triggering_event="run.extracting",
                    reason=f"acquired {candidate_count} candidates",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("acquisition", str(exc))

            return StageResult.ok(
                "acquisition",
                f"executed {len(queries)} queries, {candidate_count} candidates",
                details={
                    ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                    ContextKeys.CANDIDATE_COUNT: candidate_count,
                    ContextKeys.SUCCESSFUL_URLS: successful_urls,
                },
            )

        # No candidates — go directly to coverage review
        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision,
                idempotency_key=f"stage:acquisition_empty:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="AcquisitionStage",
                triggering_event="run.coverage_review",
                reason="no candidates acquired, reviewing coverage",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("acquisition", str(exc))

        return StageResult.ok(
            "acquisition",
            f"executed {len(queries)} queries, 0 candidates (empty)",
            details={
                ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                ContextKeys.CANDIDATE_COUNT: 0,
                ContextKeys.SUCCESSFUL_URLS: 0,
            },
        )


class ExtractionStage:
    """Extract content from acquired candidates.

    Transitions: extracting -> (indexing | coverage_review)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        config: StoreConfig,
        corpus_service: Any | None = None,
        extraction_service: Any | None = None,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.config = config
        self.corpus_service = corpus_service
        self.extraction_service = extraction_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("extracting", "coverage_review"):
            return StageResult.failed(
                "extraction",
                f"extraction stage requires extracting/coverage_review state, got {run_state}",
            )

        raw_requests = list(context.get("raw_ingest_requests") or [])
        if not self.corpus_service or not self.extraction_service:
            return StageResult.failed(
                "extraction", "authoritative corpus/extraction services unavailable"
            )
        if not raw_requests:
            return StageResult.failed(
                "extraction", "candidates contain no authoritative scraped content"
            )

        from dataclasses import replace

        attempt_by_ordinal: dict[int, dict[str, Any]] = {}
        for ordinal, item in enumerate(raw_requests):
            metadata = item.get("metadata", {})
            candidate_raw = metadata.get("candidate_id")
            if not candidate_raw:
                return StageResult.failed(
                    "extraction", "extraction request is missing candidate provenance"
                )
            candidate_id = UUID(str(candidate_raw))
            attempt_id = self.extraction_service.create_attempt(
                candidate_id=candidate_id,
                run_id=run_id,
                method="firecrawl_main_content",
                method_version="cli-1.19.27",
                requested_format="markdown",
            )
            request = item.get("request")
            raw_blob = normalized_blob = None
            if request is not None:
                raw_blob = self.extraction_service.store_raw_blob(request.content)
                normalized = request.normalized_content or request.content
                normalized_blob = self.extraction_service.store_normalized_blob(
                    normalized
                )
                item["request"] = replace(request, extraction_attempt_id=attempt_id)
            attempt_by_ordinal[ordinal] = {
                "attempt_id": attempt_id,
                "candidate_id": candidate_id,
                "raw_blob": raw_blob,
                "normalized_blob": normalized_blob,
                "metadata": metadata,
            }

        invocation_id = f"extract:{run_id}:w{context.get(ContextKeys.WAVE_COUNT, 0)}"
        run_status = self.run_service.status(run_id=run_id)
        if not run_status.external_id:
            return StageResult.failed(
                "extraction", "research run has no external ID for asset linkage"
            )
        try:
            manifest = self.corpus_service.ingest_batch(
                invocation_id=invocation_id,
                operation="orchestration_extract",
                requests=raw_requests,
                research_run_external_id=run_status.external_id,
                metadata={"run_id": str(run_id), "authority": "firecrawl-cli-1.19.27"},
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "extraction", f"authoritative corpus ingestion failed: {exc}"
            )

        completed_assets: list[dict[str, Any]] = []
        wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
        targets = context.get("candidate_coverage_items", {})
        for asset in manifest.get("assets", []):
            ordinal = int(asset["ordinal"])
            attempt = attempt_by_ordinal[ordinal]
            succeeded = asset.get("status") == "complete"
            self.extraction_service.complete_attempt(
                attempt_id=attempt["attempt_id"],
                exit_status="succeeded" if succeeded else "failed",
                raw_blob=attempt["raw_blob"],
                normalized_blob=attempt["normalized_blob"],
                parser_used=self.config.parser_version if succeeded else None,
                failure_class=(
                    "none"
                    if succeeded
                    else _extraction_failure_class(asset.get("error"))
                ),
                http_status=attempt["metadata"].get("firecrawl", {}).get("status_code"),
                backend_status=asset.get("status"),
                error_message=asset.get("error"),
            )
            candidate_id = attempt["candidate_id"]
            source_url = asset.get("requested_url")
            for item_id in targets.get(str(candidate_id), []):
                self.coverage_service.apply_extraction_attempted(
                    run_id,
                    UUID(item_id),
                    source_url=source_url,
                    extraction_status="success" if succeeded else "failed",
                    idempotency_key=(
                        f"extract:{run_id}:w{wave_count}:{candidate_id}:{item_id}"
                    ),
                )
            if not succeeded:
                continue
            if not asset.get("snapshot_id") or not asset.get("chunk_ids"):
                return StageResult.failed(
                    "extraction", "complete corpus asset lacks snapshot/chunk identity"
                )
            self.extraction_service.select_final_attempt(
                candidate_id=candidate_id,
                attempt_id=attempt["attempt_id"],
                selection_reason="authoritative Firecrawl markdown persisted",
            )
            authoritative_asset = {
                **asset,
                "candidate_id": str(candidate_id),
                "extraction_attempt_id": str(attempt["attempt_id"]),
            }
            completed_assets.append(authoritative_asset)
            for item_id in targets.get(str(candidate_id), []):
                self.coverage_service.apply_asset_acquired(
                    run_id,
                    UUID(item_id),
                    source_url=source_url,
                    idempotency_key=(
                        f"acquired:{run_id}:w{wave_count}:{candidate_id}:{item_id}"
                    ),
                )

        # Finalize the ingestion batch only after every constituent has a
        # persisted terminal outcome so batch timing and summaries are
        # derived from authoritative evidence rather than wall-clock guesses.
        # Skip when no active requests produced a batch (all preflighted away).
        if "batch_id" in manifest:
            success_count = sum(
                1 for a in manifest.get("assets", []) if a.get("status") == "complete"
            )
            failure_count = len(manifest.get("assets", [])) - success_count
            batch_status = (
                "complete"
                if failure_count == 0
                else ("failed" if success_count == 0 else "partial")
            )
            try:
                self.corpus_service.finalize_ingestion_batch(
                    manifest["batch_id"], batch_status
                )
            except Exception as exc:  # noqa: BLE001
                return StageResult.failed(
                    "extraction", f"authoritative batch finalization failed: {exc}"
                )

        extraction_success_count = len(completed_assets)
        context.setdefault("extracted_assets", []).extend(completed_assets)
        context[ContextKeys.EXTRACTION_SUCCESS_COUNT] = extraction_success_count
        context[ContextKeys.EXTRACTION_ATTEMPTS] = len(raw_requests)

        # Transition to indexing if we have content, otherwise to coverage_review
        if extraction_success_count > 0:
            try:
                self.run_service.transition(
                    run_id,
                    "indexing",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:extraction_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="ExtractionStage",
                    triggering_event="run.indexing",
                    reason=f"extraction succeeded for {extraction_success_count} sources",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("extraction", str(exc))
        else:
            try:
                self.run_service.transition(
                    run_id,
                    "coverage_review",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:extraction_empty:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="ExtractionStage",
                    triggering_event="run.coverage_review",
                    reason="no successful extractions, reviewing coverage",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("extraction", str(exc))

        return StageResult.ok(
            "extraction",
            f"{extraction_success_count} successful extractions",
            details={
                ContextKeys.EXTRACTION_SUCCESS_COUNT: extraction_success_count,
                ContextKeys.EXTRACTION_ATTEMPTS: len(raw_requests),
                "extracted_assets": completed_assets,
            },
        )


class IndexingStage:
    """Build and activate vector index.

    Transitions: indexing -> coverage_review
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
        corpus_service: Any | None = None,
    ) -> None:
        self.run_service = run_service
        self.config = config
        self.corpus_service = corpus_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "indexing":
            return StageResult.failed(
                "indexing",
                f"indexing stage requires indexing state, got {run_state}",
            )

        index_build_id = str(uuid4())
        index_fingerprint = "default_vector_index"

        if not (
            self.corpus_service
            and getattr(self.corpus_service, "index", None)
            and getattr(self.corpus_service, "embedder", None)
        ):
            return StageResult.failed(
                "indexing",
                "vector index and embedding services are required",
            )

        try:
            from .indexing import IndexWorker

            worker = IndexWorker(
                uow_factory=self.corpus_service.uow_factory,
                index=self.corpus_service.index,
                embedder=self.corpus_service.embedder,
                queue=getattr(self.corpus_service, "queue", None),
            )
            entity_ids = list(
                dict.fromkeys(
                    UUID(str(chunk_id))
                    for asset in context.get("extracted_assets", [])
                    for chunk_id in asset.get("chunk_ids", [])
                )
            )
            if not entity_ids:
                return StageResult.failed(
                    "indexing", "no exact-run chunks are available for indexing"
                )
            batch_result = {
                "claimed": 0,
                "complete": 0,
                "failed": 0,
                "lease_lost": 0,
                "embedding_batches": 0,
                "embedding_texts": 0,
                "embedding_elapsed_seconds": 0.0,
            }
            index_deadline = time.monotonic() + 30.0
            while True:
                current = worker.run_batch(
                    limit=self.corpus_service.config.embedding_batch_size,
                    entity_ids=entity_ids,
                )
                for name in batch_result:
                    value = current.get(name, 0)
                    if name == "embedding_elapsed_seconds":
                        batch_result[name] += float(value)
                    else:
                        batch_result[name] += int(value)
                if current.get("failed", 0) or current.get("lease_lost", 0):
                    break
                if current.get("claimed", 0) == 0:
                    fingerprint = getattr(
                        self.corpus_service.embedder,
                        "fingerprint",
                        index_fingerprint,
                    )
                    with self.corpus_service.uow_factory() as uow:
                        indexed_count = uow.index_jobs.count_complete_manifests(
                            entity_ids, fingerprint
                        )
                    if indexed_count == len(entity_ids):
                        break
                    if time.monotonic() >= index_deadline:
                        break
                    time.sleep(0.25)
            elapsed = float(batch_result["embedding_elapsed_seconds"])
            if batch_result.get("failed", 0) or batch_result.get("lease_lost", 0):
                return StageResult.failed(
                    "indexing",
                    f"vector indexing did not complete cleanly: {batch_result}",
                )
            fingerprint = getattr(
                self.corpus_service.embedder, "fingerprint", index_fingerprint
            )
            with self.corpus_service.uow_factory() as uow:
                indexed_count = uow.index_jobs.count_complete_manifests(
                    entity_ids, fingerprint
                )
            if indexed_count != len(entity_ids):
                return StageResult.failed(
                    "indexing",
                    "exact-run index manifest count mismatch: "
                    f"expected={len(entity_ids)} complete={indexed_count}",
                )

            measured_texts = batch_result["complete"]
            measured_vectors = batch_result["complete"]
            if measured_texts == 0:
                # Reused complete manifests do not execute an embedding call.
                # Take one exact-run, bounded embedding sample so strict
                # throughput remains a measured observation rather than a
                # sentinel zero or host-wide fallback.
                sample_ids = entity_ids[: self.config.embedding_batch_size]
                with self.corpus_service.uow_factory() as uow:
                    records = uow.chunks.chunks_for_index(sample_ids)
                texts = [record["text"] for record in records]
                if not texts:
                    return StageResult.failed(
                        "indexing", "no exact-run texts available for telemetry"
                    )
                sample_started = time.monotonic()
                vectors = self.corpus_service.embedder.batch(texts)
                elapsed = time.monotonic() - sample_started
                if len(vectors) != len(texts):
                    return StageResult.failed(
                        "indexing", "embedding telemetry sample was incomplete"
                    )
                batch_result["embedding_batches"] = 1
                measured_texts = len(texts)
                measured_vectors = len(vectors)
            from .telemetry_service import PerformanceTelemetryService

            with self.corpus_service.uow_factory() as uow:
                PerformanceTelemetryService(uow.connection).record_embedding_throughput(
                    run_id,
                    "indexing",
                    batch_count=int(batch_result["embedding_batches"]),
                    vector_count=measured_vectors,
                    failed_count=batch_result["failed"],
                    total_texts=measured_texts,
                    elapsed_seconds=elapsed,
                    endpoint_url=self.config.embedding_url,
                    endpoint_model=self.config.embedding_model,
                    dimension=self.config.embedding_dimension,
                )
            index_fingerprint = getattr(
                self.corpus_service.embedder, "fingerprint", index_fingerprint
            )
            logger.info("indexing batch completed: %s", batch_result)
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "indexing", f"vector indexing worker failed: {exc}"
            )

        context[ContextKeys.INDEX_BUILD_ID] = index_build_id
        context[ContextKeys.INDEX_FINGERPRINT] = index_fingerprint

        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision,
                idempotency_key=f"stage:indexing_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="IndexingStage",
                triggering_event="run.coverage_review",
                reason="indexing complete, evaluating coverage",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("indexing", str(exc))

        return StageResult.ok(
            "indexing",
            "indexing complete, transitioned to coverage_review",
            details={
                ContextKeys.INDEX_BUILD_ID: index_build_id,
                ContextKeys.INDEX_FINGERPRINT: index_fingerprint,
            },
        )


class EvidencePreparationStage:
    """Create authoritative claims, bindings, and a validated packet."""

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        config: StoreConfig,
        corpus_service: Any | None,
        evidence_service: Any | None = None,
        host_artifact_supplier: Any = None,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.config = config
        self.host_artifact_supplier = host_artifact_supplier
        self.corpus_service = corpus_service
        self.evidence_service = evidence_service or getattr(
            run_service, "evidence_service", None
        )

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "coverage_review":
            return StageResult.failed(
                "evidence_preparation",
                f"evidence preparation requires coverage_review state, got {run_state}",
            )
        if self.corpus_service is None:
            return StageResult.failed(
                "evidence_preparation", "authoritative corpus service unavailable"
            )
        if self.evidence_service is None:
            return StageResult.failed(
                "evidence_preparation", "authoritative evidence service unavailable"
            )

        from .evidence_preparation_service import (
            EvidencePreparationError,
            EvidencePreparationService,
        )
        from .semantic_service import SemanticCallService

        service = EvidencePreparationService(
            corpus_service=self.corpus_service,
            evidence_service=self.evidence_service,
            coverage_service=self.coverage_service,
            semantic_service=SemanticCallService(
                self.run_service.uow_factory,
                host_artifact_supplier=self.host_artifact_supplier,
            ),
            config=self.config,
        )
        try:
            prepared = service.prepare(
                run_id=run_id,
                run_revision=run_revision,
                spec=context["spec"],
                research_spec_id=UUID(str(context["spec"]["research_spec_id"])),
                coverage_revision=coverage_revision or 1,
                extracted_assets=context.get("extracted_assets", []),
                coverage_items=context.get("coverage_items", []),
            )
        except (EvidencePreparationError, KeyError, ValueError) as exc:
            return StageResult.failed("evidence_preparation", str(exc))

        context["evidence_packet_revision"] = prepared.packet_revision
        return StageResult.ok(
            "evidence_preparation",
            "authoritative EvidencePacket validated",
            details={
                "evidence_packet_revision": prepared.packet_revision,
                "claim_count": prepared.claim_count,
                "binding_count": prepared.binding_count,
                "passage_count": prepared.passage_count,
            },
        )


class CoverageReviewStage:
    """Evaluate coverage and propose next action.

    Transitions: coverage_review -> (acquiring | extracting | retrieving |
    synthesizing | partial | failed)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "coverage_review":
            return StageResult.failed(
                "coverage_review",
                f"coverage_review stage requires coverage_review state, got {run_state}",
            )

        # Rebuild coverage projection
        try:
            ledger = self.coverage_service.rebuild_projection(
                run_id,
                idempotency_key=f"rebuild:{run_id}:{coverage_revision or 0}",
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "coverage_review", f"projection rebuild failed: {exc}"
            )

        overall_status = ledger.overall_status.value if ledger else "unassessed"

        # Create snapshot of current coverage
        try:
            new_coverage_revision = (coverage_revision or 0) + 1
            self.coverage_service.create_snapshot(
                run_id,
                ledger={
                    "schema_version": "coverage-ledger-v1",
                    "run_id": str(run_id),
                    "revision": new_coverage_revision,
                    "items": [
                        {
                            "coverage_item_id": str(item.coverage_item_id),
                            "item_type": item.item_type.value,
                            "subject_id": item.subject_id,
                            "status": item.status.value,
                            "candidate_ids": [str(cid) for cid in item.candidate_ids],
                            "snapshot_ids": [str(sid) for sid in item.snapshot_ids],
                            "passage_ids": [str(pid) for pid in item.passage_ids],
                            "independent_source_count": item.independent_source_count,
                            "required_independent_source_count": item.required_independent_source_count,
                            "authority_classes_present": list(
                                item.authority_classes_present
                            ),
                            "freshness_status": item.freshness_status.value,
                            "remaining_gap": item.remaining_gap,
                            "confidence": item.confidence,
                        }
                        for item in ledger.items
                    ],
                    "overall_status": overall_status,
                },
                coverage_revision=new_coverage_revision,
                idempotency_key=f"snapshot:review:{run_id}:{new_coverage_revision}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("coverage snapshot creation failed: %s", exc)

        # Update context with coverage results
        context[ContextKeys.COVERAGE_LEDGER] = ledger
        context[ContextKeys.COVERAGE_STATUS] = overall_status
        context[ContextKeys.OVERALL_STATUS] = overall_status

        # Determine next action based on coverage and terminal decision policy
        budget_exhausted = context.get("_budget_exhausted", False)
        no_progress = context.get("_no_progress", False)

        decision_type, reason = _coverage_decision(
            overall_status,
            budget_exhausted=budget_exhausted,
            no_progress=no_progress,
        )

        # Check if terminal outcome was set by TerminalDecisionPolicy
        terminal_outcome = context.get("_terminal_outcome")
        if terminal_outcome:
            if terminal_outcome == TerminalDecisionOutcome.SUFFICIENT.value:
                decision_type = STRATEGY_DECISION_SYNTHESIZE
                reason = context.get("_terminal_reason", "sufficient coverage")
            elif terminal_outcome == TerminalDecisionOutcome.FAILED.value:
                decision_type = STRATEGY_DECISION_FAIL
                reason = context.get("_terminal_reason", "no-progress or loop detected")
            elif terminal_outcome in (
                TerminalDecisionOutcome.PARTIAL.value,
                TerminalDecisionOutcome.BLOCKED.value,
            ):
                decision_type = STRATEGY_DECISION_PARTIAL
                reason = context.get(
                    "_terminal_reason", "partial coverage or blocked requirement"
                )

        # Map decision type to run state name for transitions
        state_name = decision_to_state(decision_type)

        # Handle terminal decision paths (failed or partial) directly
        if state_name in ("failed", "partial"):
            try:
                if state_name == "failed":
                    self.run_service.fail(
                        run_id,
                        expected_revision=run_revision,
                        idempotency_key=f"stage:coverage_review:failed:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="CoverageReviewStage",
                        reason=f"coverage {overall_status} -> failed ({reason})",
                        outcome="failed",
                    )
                else:
                    self.run_service.partial(
                        run_id,
                        expected_revision=run_revision,
                        idempotency_key=f"stage:coverage_review:partial:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="CoverageReviewStage",
                        reason=f"coverage {overall_status} -> partial ({reason})",
                        outcome="partial",
                    )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("coverage_review", str(exc))

            return StageResult.terminal(
                "coverage_review",
                f"coverage {overall_status}, next action: {state_name} ({reason})",
                details={
                    ContextKeys.COVERAGE_STATUS: overall_status,
                    ContextKeys.OVERALL_STATUS: overall_status,
                    ContextKeys.NEXT_ACTION: state_name,
                    ContextKeys.STRATEGY_PROPOSAL_ID: None,
                },
            )

        # Propose the next action through the strategy service for non-terminal decisions
        proposal_id = self._propose_next_action(
            run_id, run_revision, new_coverage_revision, decision_type, reason, context
        )

        # If authorization was rejected for a non-terminal decision, fail.
        if proposal_id is None:
            return StageResult.failed(
                "coverage_review",
                "strategy authorization rejected — cannot proceed",
            )

        # Transition to the non-terminal state (acquiring or synthesizing)
        try:
            self.run_service.transition(
                run_id,
                state_name,
                expected_revision=run_revision,
                idempotency_key=f"stage:coverage_review:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="CoverageReviewStage",
                triggering_event="run.coverage_review_decision",
                reason=f"coverage {overall_status} -> {state_name} ({reason})",
                completion={
                    "coverage_status": overall_status,
                    "next_action": state_name,
                },
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("coverage_review", str(exc))

        result = StageResult.ok(
            "coverage_review",
            f"coverage {overall_status}, next action: {state_name} ({reason})",
            details={
                ContextKeys.COVERAGE_STATUS: overall_status,
                ContextKeys.OVERALL_STATUS: overall_status,
                ContextKeys.NEXT_ACTION: state_name,
                ContextKeys.STRATEGY_PROPOSAL_ID: str(proposal_id)
                if proposal_id
                else None,
            },
        )

        # If synthesizing, return a terminal result to proceed to synthesis
        if state_name == "synthesizing":
            return StageResult.terminal(
                "coverage_review",
                f"coverage {overall_status}, next action: {state_name} ({reason})",
                details={
                    ContextKeys.COVERAGE_STATUS: overall_status,
                    ContextKeys.OVERALL_STATUS: overall_status,
                    ContextKeys.NEXT_ACTION: state_name,
                    ContextKeys.STRATEGY_PROPOSAL_ID: str(proposal_id)
                    if proposal_id
                    else None,
                },
            )

        return result

    def _propose_next_action(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        reason: str,
        context: dict[str, Any],
    ) -> UUID | None:
        """Create and authorize a strategy proposal for the next action.

        Returns the proposal ID if authorized, or None if authorization
        is rejected or authorization fails.
        """
        target_items = []
        ledger = context.get(ContextKeys.COVERAGE_LEDGER)
        if ledger:
            target_items = [
                str(item.coverage_item_id)
                for item in ledger.items
                if item.status.value not in ("satisfied", "waived")
            ]

        # Synthesis still requires an explicit target set for provenance. When
        # coverage is complete, target the satisfied items that the report is
        # authorized to synthesize rather than submitting an empty proposal.
        if (
            not target_items
            and decision_type == STRATEGY_DECISION_SYNTHESIZE
            and ledger
        ):
            target_items = [str(item.coverage_item_id) for item in ledger.items]

        if not target_items and decision_type == STRATEGY_DECISION_SEARCH:
            return None  # No targeted items to propose for search actions

        # C5: Validate proposal before creating to avoid orphaned records
        proposed_queries = []
        if decision_type == STRATEGY_DECISION_SEARCH and target_items:
            objective = context.get("spec", {}).get("objective", "")
            unresolved = [
                item
                for item in (ledger.items if ledger else [])
                if item.status.value not in ("satisfied", "waived")
            ]
            proposed_queries = self._generate_adaptive_queries(
                objective=objective,
                unresolved_items=unresolved[:10],
            )

        try:
            validation = self.strategy_service.validate_proposal(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision_type=decision_type,
                target_coverage_item_ids=[UUID(tid) for tid in target_items[:10]],
                proposed_queries=proposed_queries,
                estimated_cost={},
                rationale=f"Next action: {decision_type} because {reason}",
                current_run_revision=run_revision,
                current_coverage_revision=coverage_revision,
                run_state="coverage_review",
                is_terminal=decision_type
                in (STRATEGY_DECISION_PARTIAL, STRATEGY_DECISION_FAIL),
                run_exists=True,
                coverage_items_exist=True,
            )
            if not validation.valid:
                logger.warning(
                    "strategy proposal rejected by validation: %s",
                    validation.rejection_reasons,
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategy proposal validation failed: %s", exc)
            return None

        try:
            proposal = self.strategy_service.create_proposal(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision_type=decision_type,
                target_coverage_item_ids=[UUID(tid) for tid in target_items[:10]],
                proposed_queries=proposed_queries,
                expected_contribution=f"coverage_{reason}",
                rationale=f"Next action: {decision_type} because {reason}",
                confidence=0.5,
                idempotency_key=f"proposal:{run_id}:{decision_type}:{coverage_revision}",
            )

            # Authorize the proposal before returning — no adaptive action
            # executes without a recorded authorization decision.
            decision = self.strategy_service.authorize(
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                current_run_revision=run_revision,
                current_coverage_revision=coverage_revision,
                run_state="coverage_review",
                is_terminal=decision_type
                in (
                    STRATEGY_DECISION_PARTIAL,
                    STRATEGY_DECISION_FAIL,
                ),
            )

            if decision.outcome != "accepted":
                logger.warning(
                    "strategy proposal %s rejected: %s",
                    proposal.proposal_id,
                    decision.rejection_reasons,
                )
                return None

            # C4: Populate context with proposal and decision IDs for downstream stages
            context[ContextKeys.STRATEGY_PROPOSAL_ID] = proposal.proposal_id
            context[ContextKeys.STRATEGY_DECISION_ID] = decision.decision_id
            context[ContextKeys.STRATEGY_DECISION] = decision_type

            # Store authorized queries for AcquisitionStage (cycle 2+)
            if decision_type == STRATEGY_DECISION_SEARCH and proposal.proposed_queries:
                existing = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
                existing.append(
                    {
                        "proposal_id": str(proposal.proposal_id),
                        "decision_type": decision_type,
                        "proposed_queries": list(proposal.proposed_queries),
                    }
                )
                context[ContextKeys.AUTHORIZED_QUERIES] = existing

            return proposal.proposal_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategy proposal creation failed: %s", exc)
            return None

    def _generate_adaptive_queries(
        self,
        objective: str,
        unresolved_items: list[Any],
    ) -> list[dict[str, str]]:
        """Generate targeted search queries for unresolved coverage gaps using LLM or gap heuristics."""
        queries: list[dict[str, str]] = []

        if not unresolved_items:
            return queries

        # Try Google API first (Vertex/Gemini format)
        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            try:
                import json
                import urllib.request

                model = os.environ.get(
                    "FIRECRAWL_QUERY_PLANNER_MODEL", "gemini-2.5-flash"
                )
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}

                gaps = [
                    getattr(item, "remaining_gap", None)
                    or getattr(item, "subject_id", "")
                    for item in unresolved_items
                ]
                prompt = (
                    f"Objective: {objective}\n"
                    f"Unresolved coverage gaps: {gaps}\n\n"
                    f"Generate up to 5 complementary natural-language search queries to resolve these coverage gaps. "
                    f"Return JSON object with 'queries': array of strings."
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": {
                            "type": "OBJECT",
                            "properties": {
                                "queries": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                }
                            },
                            "required": ["queries"],
                        },
                    },
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    text = res_data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)
                    for q in parsed.get("queries", []):
                        if isinstance(q, str) and q.strip():
                            queries.append({"query": q.strip(), "facet": "adaptive"})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Google query planning failed, trying local vLLM: %s", exc
                )

        # Fall back to local vLLM (OpenAI-compatible) when Google API is unavailable
        if not queries:
            _gen_url = os.environ.get("GENERATIVE_URL") or os.environ.get(
                "FIRECRAWL_GENERATIVE_URL"
            )
            generative_url = (_gen_url or "http://127.0.0.1:8004/v1").rstrip("/")
            try:
                import json
                import urllib.request

                prompt = (
                    f"Objective: {objective}\n"
                    f"Unresolved coverage gaps: {[getattr(item, 'subject_id', '') for item in unresolved_items]}\n\n"
                    f"Generate up to 5 complementary natural-language search queries to resolve these coverage gaps. "
                    f"Return ONLY a JSON object with a 'queries' key containing an array of strings. No other text."
                )
                payload = json.dumps(
                    {
                        "model": "chat",
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    }
                ).encode()
                req = urllib.request.Request(
                    f"{generative_url}/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    text = res_data["choices"][0]["message"]["content"]
                    parsed = json.loads(text)
                    for q in parsed.get("queries", []):
                        if isinstance(q, str) and q.strip():
                            queries.append({"query": q.strip(), "facet": "adaptive"})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Local vLLM query planning failed, falling back to gap heuristic: %s",
                    exc,
                )

        if not queries and unresolved_items:
            for item in unresolved_items[:10]:
                gap_text = getattr(item, "remaining_gap", None) or getattr(
                    item, "subject_id", ""
                )
                if gap_text:
                    clean_query = gap_text.strip()
                    if clean_query.startswith("item_"):
                        item_type_str = (
                            item.item_type.value
                            if hasattr(item.item_type, "value")
                            else str(item.item_type)
                        )
                        clean_query = f"{item_type_str} {clean_query}"
                    queries.append({"query": clean_query, "facet": "adaptive"})
                else:
                    queries.append(
                        {
                            "query": f"research item {getattr(item, 'coverage_item_id', 'gap')}",
                            "facet": "adaptive",
                        }
                    )

        return queries


class NextActionStage:
    """Execute the authorized next action.

    This stage is a lightweight dispatcher that delegates to the
    appropriate service based on the coverage_review decision.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        strategy_service: StrategyRevisionService,
    ) -> None:
        self.run_service = run_service
        self.strategy_service = strategy_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("acquiring", "extracting", "retrieving", "synthesizing"):
            return StageResult.failed(
                "next_action",
                f"next_action stage requires acquiring/extracting/retrieving/synthesizing state, got {run_state}",
            )

        # Validate the strategy decision before executing
        decision_id = context.get(ContextKeys.STRATEGY_DECISION_ID)
        if decision_id:
            try:
                decision = self.strategy_service.get_decision(run_id, decision_id)
                if decision.outcome != "accepted":
                    return StageResult.failed(
                        "next_action",
                        f"strategy decision {decision_id} was rejected: {decision.rejection_reasons}",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("strategy decision validation failed: %s", exc)

        # The actual work is delegated to the appropriate service.
        # This stage exists primarily for observability and audit.
        return StageResult.ok(
            "next_action",
            f"executing {run_state} action",
            details={ContextKeys.NEXT_ACTION: run_state},
        )


class SynthesisStage:
    """Execute bounded autonomous-local synthesis via ReportService.

    Transitions: synthesizing -> validating -> completed/partial/failed

    This replaces the previous no-op SynthesisStage.  The actual synthesis
    work is delegated to ``ReportService.run_synthesis()`` which decomposes
    the work into four bounded stages (outline, binding, draft, citation_pass).
    Each stage persists its semantic call and artifact.  Failed stages can be
    retried and completed stages are skipped on resume.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
        resource_governor: Any = None,
        evidence_service: Any | None = None,
        host_artifact_supplier: Any = None,
    ) -> None:
        self.run_service = run_service
        self.config = config
        self._resource_governor = resource_governor
        self.host_artifact_supplier = host_artifact_supplier
        self._evidence_service = evidence_service or getattr(
            run_service, "evidence_service", None
        )

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "synthesizing":
            return StageResult.failed(
                "synthesis",
                f"synthesis stage requires synthesizing state, got {run_state}",
            )
        if self._evidence_service is None:
            return StageResult.failed(
                "synthesis", "authoritative evidence service unavailable"
            )

        # Build the ReportService.
        from .report_service import (
            CommercialFallbackError,
            LocalSynthesisService,
            ReportServiceError,
        )
        from .semantic_service import SemanticCallService

        semantic_service = SemanticCallService(
            self.run_service.uow_factory,
            host_artifact_supplier=self.host_artifact_supplier,
        )
        report_service = LocalSynthesisService(
            semantic_service=semantic_service,
            evidence_service=self._evidence_service,
            config=self.config,
            resource_governor=self._resource_governor,
        )

        # Run the bounded synthesis pipeline.
        packet_revision = context.get("evidence_packet_revision")
        if not isinstance(packet_revision, int) or packet_revision < 1:
            return StageResult.failed(
                "synthesis", "validated EvidencePacket revision is unavailable"
            )
        try:
            summary = report_service.run_synthesis(
                run_id=run_id,
                packet_revision=packet_revision,
                model_name=self.config.generative_model,
            )
        except CommercialFallbackError as exc:
            return StageResult.failed("synthesis", str(exc))
        except ReportServiceError as exc:
            logger.error("synthesis pipeline failed: %s", exc)
            return StageResult.failed("synthesis", str(exc))

        overall_status = summary.get("overall_status", "failed")
        if overall_status == "completed":
            # All stages completed successfully — transition to validating.
            try:
                self.run_service.transition(
                    run_id,
                    "validating",
                    expected_revision=run_revision,
                    idempotency_key=(f"stage:synthesis_done:{run_id}:{uuid4()}"),
                    actor_type="orchestrator",
                    actor_identifier="SynthesisStage",
                    triggering_event="run.validating",
                    reason="synthesis complete, entering validation",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("synthesis", str(exc))

            return StageResult.ok(
                "synthesis",
                "synthesis complete, transitioned to validating",
                details={
                    ContextKeys.SYNTHESIS_ARTIFACT_ID: str(run_id),
                    ContextKeys.REPORT_ID: str(run_id),
                    "synthesis_summary": summary,
                },
            )

        # Partial failure — some stages completed, some failed.
        stage_results = summary.get("stages", {})
        completed = sum(
            1 for s in stage_results.values() if s.get("status") == "completed"
        )
        failed = sum(1 for s in stage_results.values() if s.get("status") == "failed")
        summary_text = f"synthesis partial: {completed} completed, {failed} failed"
        error = summary.get("error", "unknown")

        return StageResult.degraded(
            "synthesis",
            summary_text,
            details={
                ContextKeys.SYNTHESIS_ARTIFACT_ID: str(run_id),
                ContextKeys.REPORT_ID: str(run_id),
                "synthesis_summary": summary,
                "synthesis_error": error,
            },
        )


class TerminalStage:
    """Handle terminal outcomes (completed, partial, failed).

    This stage records the final outcome and emits terminal events.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
    ) -> None:
        self.run_service = run_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        outcome = context.get("_terminal_outcome", "partial")

        if run_state not in ("validating", "partial", "failed"):
            return StageResult.failed(
                "terminal",
                f"terminal stage requires validating/partial/failed state, got {run_state}",
            )

        if run_state == "validating":
            try:
                if outcome == "completed":
                    self.run_service.complete(
                        run_id,
                        expected_revision=run_revision,
                        idempotency_key=f"terminal:completed:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="TerminalStage",
                        reason=context.get("_terminal_reason", "sufficient coverage"),
                        outcome="completed",
                    )
                elif outcome == "partial":
                    self.run_service.partial(
                        run_id,
                        expected_revision=run_revision,
                        idempotency_key=f"terminal:partial:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="TerminalStage",
                        reason=context.get("_terminal_reason", "partial coverage"),
                        outcome="partial",
                    )
                else:
                    self.run_service.fail(
                        run_id,
                        expected_revision=run_revision,
                        idempotency_key=f"terminal:failed:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="TerminalStage",
                        reason=context.get("_terminal_reason", "research failed"),
                        outcome="failed",
                    )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("terminal", str(exc))
        elif run_state == "partial":
            pass  # Already terminal
        else:
            pass  # Already terminal

        return StageResult.terminal(
            "terminal",
            f"run ended in {run_state} state",
            details={"outcome": outcome},
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ResearchOrchestrator:
    """Coverage-led research orchestrator.

    This is the single entry point for the coverage-led workflow.  It
    coordinates the staged pipeline and ensures that:

    * All state transitions flow through ``ResearchRunService``.
    * Coverage is evaluated after each meaningful wave.
    * Adaptive actions are proposed and authorized through
      ``StrategyRevisionService``.
    * Every invocation and transition is persisted.
    * The orchestrator can resume a run after process restart.

    Example usage::

        orchestrator = ResearchOrchestrator.build(config)
        result = orchestrator.run(
            run_id=run_id,
            spec=spec,
            search_plan=search_plan,
        )
        print(result.outcome)  # "completed", "partial", "failed"
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        acquisition_service: AcquisitionService,
        config: StoreConfig,
        corpus_service: Any | None = None,
        terminal_config: TerminalDecisionConfig | None = None,
        terminal_service: TerminalDecisionService | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        extraction_service: Any | None = None,
        evidence_service: Any | None = None,
        acquisition_stage_cls: type[AcquisitionStage] | None = None,
        extraction_stage_cls: type[ExtractionStage] | None = None,
        indexing_stage_cls: type[IndexingStage] | None = None,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.acquisition_service = acquisition_service
        self.config = config
        self.orchestrator_config = orchestrator_config or OrchestratorConfig()
        self.corpus_service = corpus_service
        self._terminal_config = terminal_config or TerminalDecisionConfig()
        # B5: Terminal-decision persistence service
        self._terminal_decision_service = terminal_service

        # Stage instances (injected classes default to module-level base classes)
        self._planning = PlanningStage(run_service, config)
        self._corpus_review = CorpusReviewStage(run_service, coverage_service)
        _acq_cls = acquisition_stage_cls or AcquisitionStage
        self._acquisition = _acq_cls(
            run_service, acquisition_service, coverage_service, strategy_service, config
        )
        _ext_cls = extraction_stage_cls or ExtractionStage
        self._extraction = _ext_cls(
            run_service,
            coverage_service,
            config,
            corpus_service=corpus_service,
            extraction_service=extraction_service,
        )
        _idx_cls = indexing_stage_cls or IndexingStage
        self._indexing = _idx_cls(run_service, config, corpus_service=corpus_service)
        self._evidence_preparation = EvidencePreparationStage(
            run_service,
            coverage_service,
            config,
            corpus_service,
            evidence_service=evidence_service,
            host_artifact_supplier=self.orchestrator_config.host_artifact_supplier,
        )
        self._coverage_review = CoverageReviewStage(
            run_service, coverage_service, strategy_service, config
        )
        self._next_action = NextActionStage(run_service, strategy_service)
        self._synthesis = SynthesisStage(
            run_service,
            config,
            resource_governor=self.orchestrator_config.resource_governor,
            evidence_service=evidence_service,
            host_artifact_supplier=self.orchestrator_config.host_artifact_supplier,
        )
        self._terminal = TerminalStage(run_service)

        # Stage registry
        self._stages: dict[str, StageHandler] = {
            "planning": self._planning,
            "corpus_review": self._corpus_review,
            "acquisition": self._acquisition,
            "extraction": self._extraction,
            "indexing": self._indexing,
            "evidence_preparation": self._evidence_preparation,
            "coverage_review": self._coverage_review,
            "next_action": self._next_action,
            "synthesis": self._synthesis,
            "terminal": self._terminal,
        }

    @classmethod
    def build(
        cls,
        config: StoreConfig | None = None,
        *,
        orchestrator_config: OrchestratorConfig | None = None,
        corpus_service: Any | None = None,
        terminal_config: Any | None = None,
        acquisition_stage_cls: type[AcquisitionStage] | None = None,
        extraction_stage_cls: type[ExtractionStage] | None = None,
        indexing_stage_cls: type[IndexingStage] | None = None,
    ) -> ResearchOrchestrator:
        """Build an orchestrator with all required services.

        Args:
            config: Store configuration.  Defaults to env-based config.
            orchestrator_config: Orchestrator-specific settings.
            corpus_service: Optional CorpusService instance.
            terminal_config: Optional TerminalDecisionConfig override.
                Defaults to ``TerminalDecisionConfig.load()`` (env-var tuned).
            acquisition_stage_cls: Stage class for acquisition (defaults to base).
            extraction_stage_cls: Stage class for extraction (defaults to base).
            indexing_stage_cls: Stage class for indexing (defaults to base).

        Returns:
            A fully wired ``ResearchOrchestrator`` instance.
        """
        from .container import (
            build_acquisition_service,
            build_evidence_service,
            build_run_service,
            build_service,
            build_strategy_service,
        )

        config = config or StoreConfig.from_env()
        config.require_database()
        orchestrator_config = orchestrator_config or OrchestratorConfig()

        run_service = build_run_service(config)
        acquisition_service = build_acquisition_service(config)
        strategy_service = build_strategy_service(config)
        coverage_service = CoverageService(run_service.uow_factory)
        if corpus_service is None:
            try:
                corpus_service = build_service(config)
            except Exception as exc:  # noqa: BLE001
                logger.debug("corpus_service auto-build deferred: %s", exc)

        extraction_service = None
        try:
            from .container import build_extraction_service

            extraction_service = build_extraction_service(config)
        except Exception as exc:  # noqa: BLE001
            logger.debug("extraction_service auto-build deferred: %s", exc)

        # N1: Load terminal config from env vars (or use explicit override)
        if terminal_config is None:
            from .terminal_decision import TerminalDecisionConfig

            terminal_config = TerminalDecisionConfig.load()

        # B5: Build terminal-decision persistence service
        terminal_service = TerminalDecisionService(run_service.uow_factory)
        evidence_service = build_evidence_service(config)

        return cls(
            run_service=run_service,
            coverage_service=coverage_service,
            strategy_service=strategy_service,
            acquisition_service=acquisition_service,
            config=config,
            corpus_service=corpus_service,
            terminal_config=terminal_config,
            terminal_service=terminal_service,
            orchestrator_config=orchestrator_config,
            extraction_service=extraction_service,
            evidence_service=evidence_service,
            acquisition_stage_cls=acquisition_stage_cls,
            extraction_stage_cls=extraction_stage_cls,
            indexing_stage_cls=indexing_stage_cls,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: UUID,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        max_adaptive_cycles: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """Execute the full coverage-led orchestration pipeline.

        This is a thin facade that delegates to the canonical lifecycle
        in ``orchestration.lifecycle.run_research``.

        Args:
            run_id: The research run UUID.
            spec: The validated ResearchSpec as a dict.
            search_plan: The validated SearchPlan as a dict.
            max_adaptive_cycles: Override the default max cycles.
            context: Additional context to pass to stages.

        Returns:
            An ``OrchestratorResult`` describing the final outcome.
        """
        from .orchestration.commands import RunResearchCommand
        from .orchestration.lifecycle import run_research

        command = RunResearchCommand(
            run_id=run_id,
            spec=spec,
            search_plan=search_plan,
            max_adaptive_cycles=max_adaptive_cycles,
            context=dict(context or {}),
        )
        return run_research(self, command)

    def run_from_external_id(
        self,
        external_id: str,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        create_if_missing: bool = True,
        **kwargs: Any,
    ) -> OrchestratorResult:
        """Run orchestration using an external run ID string.

        If ``create_if_missing`` is True and the run does not exist,
        a new run is created in the ``created`` state.

        Args:
            external_id: External run identifier.
            spec: The ResearchSpec as a dict.
            search_plan: The SearchPlan as a dict.
            create_if_missing: Create the run if it doesn't exist.
            **kwargs: Passed to ``run``.

        Returns:
            An ``OrchestratorResult``.
        """
        try:
            run_status = self.run_service.status(external_id=external_id)
            run_id = run_status.id
        except KeyError:
            if not create_if_missing:
                return OrchestratorResult(
                    run_id=UUID(int=0),
                    final_state="not_found",
                    outcome="not_found",
                    error=f"run {external_id} not found and create_if_missing=False",
                )
            run_status = self.run_service.create(
                objective=spec.get("objective", external_id),
                external_id=external_id,
                execution_mode=self.orchestrator_config.execution_mode,
            )
            run_id = run_status.id

        return self.run(run_id, spec, search_plan, **kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute_stage(
        self,
        stage_name: str,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        """Execute a single stage and record the invocation."""
        stage = self._stages.get(stage_name)
        if stage is None:
            return StageResult.failed("unknown", f"unknown stage: {stage_name}")

        # Record invocation
        try:
            # Include wave count in idempotency key for multi-cycle runs
            wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
            self.run_service.record_search_response(
                run_id,
                query_text=f"stage:{stage_name}",
                backend="orchestrator",
                raw_payload=f"stage invocation: {stage_name}",
                idempotency_key=f"invocation:{stage_name}:{run_id}:w{wave_count}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "stage invocation recording failed for %s: %s", stage_name, exc
            )

        start = time.monotonic()
        result = stage.execute(
            run_id, run_revision, coverage_revision, run_state, context
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        # Wrap result with duration — create a new dict since StageResult is frozen
        details = dict(result.details or {})
        details["duration_ms"] = duration_ms

        logger.info(
            "stage %s: outcome=%s summary=%s duration=%dms",
            stage_name,
            result.outcome.value,
            result.summary,
            duration_ms,
        )

        return StageResult(
            stage=result.stage,
            outcome=result.outcome,
            summary=result.summary,
            details=details,
            events=result.events,
            warnings=result.warnings,
            error=result.error,
        )

    def _check_budget(self, context: dict[str, Any], run_id: UUID) -> bool:
        """Check if the hard budget has been exhausted."""
        wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
        max_cycles = context.get(
            "_max_adaptive_cycles", self.orchestrator_config.max_adaptive_cycles
        )
        return wave_count >= max_cycles

    def _check_no_progress(self, context: dict[str, Any], run_id: UUID) -> bool:
        """Check if the run has made no progress since the last cycle.

        This is a lightweight pre-check.  The full terminal decision
        policy is evaluated in ``_evaluate_terminal_decision`` which
        produces structured no-progress signals.
        """
        previous_status = context.get("_previous_coverage_status")
        current_status = context.get(ContextKeys.OVERALL_STATUS)
        if previous_status and current_status == previous_status:
            # Same status two cycles in a row — no progress
            return True
        if current_status:
            context["_previous_coverage_status"] = current_status
        return False

    def _evaluate_terminal_decision(
        self,
        context: dict[str, Any],
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
    ) -> TerminalDecision | None:
        """Evaluate the terminal decision policy.

        Returns the ``TerminalDecision`` if a terminal decision is reached,
        or ``None`` if the run should continue.

        Exception handling:

        * ``TerminalDecisionPolicyError`` (policy evaluation failure) is
          non-fatal — the orchestrator falls back to the budget check.

        This method does **not** persist the decision or update context —
        those are handled by ``ResearchRunService.commit_terminal_decision``
        in a single atomic transaction with the lifecycle transition.
        """
        try:
            policy = TerminalDecisionPolicy(self._terminal_config)

            return policy.evaluate(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                overall_status=context.get(ContextKeys.OVERALL_STATUS, "unassessed"),
                budget_exhausted=context.get("_budget_exhausted", False),
                no_progress=context.get("_no_progress", False),
                strategy_revision_count=context.get("_strategy_revision_count", 0),
                wall_clock_seconds=(
                    time.monotonic()
                    - context.get(ContextKeys.WALL_CLOCK_START, time.monotonic())
                ),
                wall_clock_limit_seconds=self._terminal_config.max_wall_clock_seconds,
                new_candidate_count=context.get("_new_candidate_count", 0),
                new_asset_count=context.get("_new_asset_count", 0),
                changed_coverage_count=context.get("_changed_coverage_count", 0),
                equivalent_proposal_count=context.get("_equivalent_proposal_count", 0),
                repeated_extraction_failures=context.get(
                    "_repeated_extraction_failures", 0
                ),
                repeated_retrieval_count=context.get("_repeated_retrieval_count", 0),
                unresolved_gap=context.get("_unresolved_gap", ""),
                unsatisfiable_source=context.get("_unsatisfiable_source", False),
            )
        except TerminalDecisionPolicyError as exc:
            # Policy evaluation errors are non-fatal — fall back to budget check.
            logger.warning(
                "terminal decision policy evaluation failed, falling back to "
                "budget check: %s",
                exc,
            )
            return None

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        """Persist a failed lifecycle state and create the result."""
        try:
            status = self.run_service.status(run_id=run_id)
            if status.state not in {"completed", "partial", "failed", "cancelled"}:
                self.run_service.fail(
                    run_id,
                    expected_revision=status.lifecycle_revision,
                    idempotency_key=f"orchestrator:failed:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="ResearchOrchestrator",
                    triggering_event="run.failed",
                    reason=error,
                    outcome="failed",
                    error=error,
                )
        except Exception:
            logger.exception(
                "could not persist failed run state for %s",
                run_id,
            )
        return OrchestratorResult(
            run_id=run_id,
            final_state="failed",
            outcome="failed",
            error=error,
        )


__all__ = [
    "STRATEGY_DECISION_FAIL",
    "STRATEGY_DECISION_PARTIAL",
    "STRATEGY_DECISION_SEARCH",
    "STRATEGY_DECISION_SYNTHESIZE",
    "AcquisitionStage",
    "ContextKeys",
    "CorpusReviewStage",
    "CoverageReviewStage",
    "ExtractionStage",
    "IndexingStage",
    "NextActionStage",
    "OrchestratorConfig",
    "OrchestratorResult",
    "PlanningStage",
    "ResearchOrchestrator",
    "StageHandler",
    "StageOutcome",
    "StageResult",
    "SynthesisStage",
    "TerminalDecisionConfig",
    "TerminalDecisionOutcome",
    "TerminalDecisionPolicy",
    "TerminalDecisionService",
    "TerminalStage",
    "_coverage_decision",
    "decision_to_state",
]
