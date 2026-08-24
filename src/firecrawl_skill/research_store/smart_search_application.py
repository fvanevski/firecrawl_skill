"""Application-owned smart-search planning and provenance operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from firecrawl_skill.research_domain.models import ResearchSpec, TimeWindow

from .budget_policy import DEFAULT_POLICY
from .semantic_service import SemanticCallService
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


def _unbounded_discovery() -> TimeWindow:
    return TimeWindow(None, None, "no bounded discovery recency", "none")


def canonical_plan(
    spec: ResearchSpec,
    queries: list[dict[str, Any]],
    *,
    discovery_window: TimeWindow | None = None,
) -> dict[str, Any]:
    """Normalize planner output without conflating evidence and discovery time."""

    question_id = spec.questions[0].question_id
    freshness = asdict(discovery_window or _unbounded_discovery())
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(queries):
        text = " ".join(str(item.get("query", "")).split())
        if not text:
            continue
        source_class = str(item.get("intended_source_class") or "unspecified")
        normalized.append(
            {
                "query_id": str(
                    uuid5(NAMESPACE_URL, f"{spec.research_spec_id}:{index}:{text}")
                ),
                "query": text,
                "facet": str(item.get("facet") or "objective"),
                "target_question_ids": [str(question_id)],
                "target_claim_ids": [],
                "intended_source_classes": [source_class],
                "expected_organizations": list(item.get("expected_organizations") or []),
                "freshness_requirement": freshness,
                "expected_contribution": str(
                    item.get("expected_contribution") or "objective coverage"
                ),
                "domain_restrictions": [],
                "negative_terms": [],
                "priority": index + 1,
            }
        )
    if not normalized:
        raise ValueError("planner produced no usable queries")
    return {
        "schema_version": "search-plan-v1",
        "research_spec_id": str(spec.research_spec_id),
        "revision": 1,
        "queries": normalized,
    }


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
    """Persist ResearchSpec, independent discovery plan, and semantic provenance."""

    budget = evaluate_budget(spec, status.lifecycle_revision)
    semantic = SemanticCallService(run_service.uow_factory)
    queries, planner_provenance = plan_queries(
        topic,
        int(budget["effective_caps"]["max_search_branches"]),
        semantic,
        {
            "run_id": str(status.id),
            "run_revision": status.lifecycle_revision,
            "stage": "planning",
            "schema_name": "research-query-plan-v1",
            "schema_version": 1,
            "artifact_type": "search_plan",
            "idempotency_key": f"smart:planner:{status.id}:r1",
            "policy_version": budget["policy_version"],
        },
        planner,
    )
    bundle = persist_planning_bundle(
        run_service,
        status.id,
        spec=spec,
        budget=budget,
        plan=canonical_plan(spec, queries, discovery_window=discovery_window),
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
