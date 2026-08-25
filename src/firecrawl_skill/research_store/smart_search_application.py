"""Application-owned smart-search planning and provenance operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain import load_model, serialize_model
from firecrawl_skill.research_domain.models import ResearchSpec, TimeWindow

from .budget_policy import DEFAULT_POLICY
from .query_policy import materialize_query_plan, semantic_query_proposals
from .semantic_service import SemanticCallService
from .smart_objective_intent import unbounded_discovery_window
from .smart_orchestrator import PlanningBundle, persist_planning_bundle

QueryPlanner = Callable[
    [str, int, SemanticCallService, dict[str, Any]],
    tuple[list[dict[str, Any]], dict[str, Any]],
]


def evaluate_budget(spec: ResearchSpec, run_revision: int) -> dict[str, Any]:
    return DEFAULT_POLICY.evaluate(
        spec,
        spec_revision=1,
        run_revision=run_revision,
        user_limits={},
    ).to_dict()


def deterministic_queries(topic: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Application fallback proposal; target IDs are bound during materialization."""

    return (
        [
            {
                "query": topic,
                "facet": "exact_objective",
                "intended_source_class": "unspecified",
                "expected_organizations": [],
                "expected_contribution": "direct evidence for the stated objective",
            }
        ],
        {"status": "degraded", "fallback": "exact_objective_only"},
    )


def canonical_plan(
    spec: ResearchSpec,
    queries: list[dict[str, Any]],
    *,
    run_id: UUID | None = None,
    discovery_window: TimeWindow | None = None,
    max_queries: int | None = None,
) -> dict[str, Any]:
    """Deterministically materialize semantic proposals into SearchPlan authority."""

    cap = max_queries if max_queries is not None else max(1, len(queries))
    return materialize_query_plan(
        spec,
        queries,
        run_id=run_id,
        discovery_window=discovery_window or unbounded_discovery_window(),
        max_queries=cap,
    )


def plan_queries(
    topic: str,
    max_queries: int,
    semantic_service: SemanticCallService,
    semantic_context: dict[str, Any],
    planner: QueryPlanner,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    queries, provenance = planner(
        topic,
        max_queries,
        semantic_service,
        semantic_context,
    )
    # #310 shipped an intentionally temporary exact-objective planner. When
    # that adapter identifies itself, #311 replaces the placeholder with the
    # versioned semantic-only proposal stage on the canonical controller path.
    if provenance.get("fallback") == "exact_objective_only":
        spec_payload = semantic_context.get("research_spec")
        if not isinstance(spec_payload, dict):
            raise ValueError("semantic query planning requires persisted ResearchSpec")
        spec = load_model(spec_payload)
        if not isinstance(spec, ResearchSpec):
            raise ValueError("semantic query planning ResearchSpec is malformed")
        semantic_queries, semantic_provenance = semantic_query_proposals(
            topic=topic,
            max_queries=max_queries,
            semantic_service=semantic_service,
            semantic_context=semantic_context,
            spec=spec,
        )
        if semantic_queries:
            return semantic_queries, {
                **semantic_provenance,
                "replaced_fallback": "exact_objective_only",
            }
        return queries, {
            **provenance,
            "semantic_proposal": semantic_provenance,
        }
    if queries:
        return queries, provenance
    fallback, fallback_provenance = deterministic_queries(topic)
    return fallback, {**provenance, **fallback_provenance}


def persist_planner_provenance(
    run_service: Any,
    run_id: UUID,
    planner_provenance: dict[str, Any],
    invocation_id: str,
    *,
    objective_intent_provenance: dict[str, Any] | None = None,
) -> None:
    with run_service.uow_factory() as uow:
        uow.runs.append_event(
            run_id,
            "planning.provenance_recorded",
            "orchestrator",
            f"smart:planning-provenance:{run_id}:r1",
            actor_identifier="fsearch_smart",
            payload={
                "planner": planner_provenance,
                "objective_intent": objective_intent_provenance or {},
                "external_invocation_id": invocation_id,
            },
        )
        uow.commit()


def initialize_planning_bundle(
    run_service: Any,
    status: Any,
    *,
    topic: str,
    spec: ResearchSpec,
    invocation_id: str,
    planner: QueryPlanner,
    discovery_window: TimeWindow | None = None,
    objective_intent_provenance: dict[str, Any] | None = None,
) -> PlanningBundle:
    """Persist semantic proposal, deterministic plan, budget, and provenance."""

    budget = evaluate_budget(spec, status.lifecycle_revision)
    semantic = SemanticCallService(
        run_service.uow_factory,
        host_artifact_supplier=getattr(run_service, "host_artifact_supplier", None),
    )
    max_queries = int(budget["effective_caps"]["max_search_branches"])
    queries, planner_provenance = plan_queries(
        topic,
        max_queries,
        semantic,
        {
            "run_id": str(status.id),
            "run_revision": status.lifecycle_revision,
            "stage": "planning",
            "schema_name": "search-query-proposal-v1",
            "schema_version": 1,
            "artifact_type": "search_query_proposal",
            "idempotency_key": f"smart:planner:{status.id}:r1",
            "policy_version": budget["policy_version"],
            "research_spec": serialize_model(spec),
        },
        planner,
    )
    bundle = persist_planning_bundle(
        run_service,
        status.id,
        spec=spec,
        budget=budget,
        plan=canonical_plan(
            spec,
            queries,
            run_id=status.id,
            discovery_window=discovery_window,
            max_queries=max_queries,
        ),
        run_revision=status.lifecycle_revision,
    )
    persist_planner_provenance(
        run_service,
        status.id,
        planner_provenance,
        invocation_id,
        objective_intent_provenance=objective_intent_provenance,
    )
    return bundle


__all__ = [
    "QueryPlanner",
    "canonical_plan",
    "deterministic_queries",
    "evaluate_budget",
    "initialize_planning_bundle",
    "persist_planner_provenance",
    "plan_queries",
]
