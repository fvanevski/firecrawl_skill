"""Smart-search adapter for exact relational search-plan provenance.

This module deliberately performs only exact, current-plan resolution. It does
not infer historical provenance and refuses ambiguous duplicate query text.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .acquisition_service import SearchProvenanceError
from .bounded_orchestrator import BoundedAcquisitionStage, BoundedExtractionStage
from .smart_orchestrator import ResumableResearchOrchestrator, SmartResumeError


class PlannedAcquisitionService:
    """Attach exact persisted plan/query IDs before delegating acquisition."""

    def __init__(
        self,
        delegate: Any,
        *,
        uow_factory: Any,
        run_id: UUID,
        plan_id: UUID,
        planned_query_texts: frozenset[str],
    ) -> None:
        self.delegate = delegate
        self.uow_factory = uow_factory
        self.run_id = UUID(str(run_id))
        self.plan_id = UUID(str(plan_id))
        self.planned_query_texts = planned_query_texts

    def execute_search(self, run_id: UUID, query_text: str, **kwargs: Any):
        run_id = UUID(str(run_id))
        if run_id != self.run_id:
            raise SearchProvenanceError(
                "planned acquisition adapter cannot cross research runs"
            )

        if query_text in self.planned_query_texts:
            plan_query_id = self._resolve_exact_plan_query(query_text)
            supplied_plan_id = kwargs.pop("plan_id", None)
            supplied_query_id = kwargs.pop("plan_query_id", None)
            if (
                supplied_plan_id is not None
                and UUID(str(supplied_plan_id)) != self.plan_id
            ):
                raise SearchProvenanceError("conflicting search plan provenance")
            if (
                supplied_query_id is not None
                and UUID(str(supplied_query_id)) != plan_query_id
            ):
                raise SearchProvenanceError("conflicting search plan query provenance")
            kwargs["plan_id"] = self.plan_id
            kwargs["plan_query_id"] = plan_query_id

        return self.delegate.execute_search(run_id, query_text, **kwargs)

    def _resolve_exact_plan_query(self, query_text: str) -> UUID:
        with self.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT id
                   FROM search_plan_queries
                   WHERE run_id=%s AND plan_id=%s AND query_text=%s
                   ORDER BY query_index,id""",
                (self.run_id, self.plan_id, query_text),
            )
            rows = cur.fetchall()
        if not rows:
            raise SearchProvenanceError(
                "persisted smart-search query has no relational plan-query row"
            )
        if len(rows) != 1:
            raise SearchProvenanceError(
                "persisted smart-search query text is ambiguous within its plan"
            )
        return UUID(str(rows[0][0]))


class ProvenanceResumableResearchOrchestrator(ResumableResearchOrchestrator):
    """Production smart orchestrator with exact plan/query acquisition linkage.

    The class builder defaults to the bounded acquisition/extraction stages so
    direct historical callers remain safe. Checkpoint behavior is inherited
    explicitly through ``ResumableResearchOrchestrator``.
    """

    @classmethod
    def build(
        cls,
        config=None,
        *,
        orchestrator_config=None,
        corpus_service=None,
        terminal_config=None,
        acquisition_stage_cls=None,
        extraction_stage_cls=None,
        indexing_stage_cls=None,
    ):
        """Build the smart production topology without import-time rebinding."""
        return super().build(
            config,
            orchestrator_config=orchestrator_config,
            corpus_service=corpus_service,
            terminal_config=terminal_config,
            acquisition_stage_cls=(acquisition_stage_cls or BoundedAcquisitionStage),
            extraction_stage_cls=(extraction_stage_cls or BoundedExtractionStage),
            indexing_stage_cls=indexing_stage_cls,
        )

    def run(
        self,
        run_id: UUID,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        max_adaptive_cycles: int | None = None,
        context: dict[str, Any] | None = None,
    ):
        ctx = dict(context or {})
        raw_plan_id = ctx.get("search_plan_id")
        if raw_plan_id is None:
            raise SmartResumeError(
                "smart acquisition requires the persisted search_plan_id"
            )
        plan_id = UUID(str(raw_plan_id))
        planned_texts = [
            str(item.get("query") or "").strip()
            for item in search_plan.get("queries", [])
            if str(item.get("query") or "").strip()
        ]
        if len(planned_texts) != len(set(planned_texts)):
            raise SmartResumeError(
                "smart search plan contains duplicate query text; relational "
                "linkage is ambiguous"
            )

        current = self._acquisition.acquisition_service
        delegate = (
            current.delegate
            if isinstance(current, PlannedAcquisitionService)
            else current
        )
        wrapped = PlannedAcquisitionService(
            delegate,
            uow_factory=self.run_service.uow_factory,
            run_id=UUID(str(run_id)),
            plan_id=plan_id,
            planned_query_texts=frozenset(planned_texts),
        )
        self._acquisition.acquisition_service = wrapped
        self.acquisition_service = wrapped

        return super().run(
            run_id,
            spec,
            search_plan,
            max_adaptive_cycles=max_adaptive_cycles,
            context=ctx,
        )
