"""Deterministic PostgreSQL-authoritative retained-corpus review for issue #310."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_domain.models import MechanicalStatus, ResearchSpec

from .assessment.coverage import CoverageService
from .evidence_preparation_service import (
    EvidencePreparationError,
    EvidencePreparationService,
)
from .research_controller_contract import (
    ControllerBlockedError,
    ControllerConfig,
    bounded_text,
)
from .run_service import ResearchRunService, RunStatus
from .semantic_service import SemanticCallService
from .smart_orchestrator import PlanningBundle
from .temporal_coverage import TemporalCoverageUnsatisfied
from .temporal_policy import (
    has_temporal_obligations,
    passage_temporally_qualifies,
)

_RETAINED_SELECTION_EVENT = "controller.retained_selection_recorded"
_RETAINED_EVALUATION_EVENT = "controller.retained_evaluation_recorded"
_MAX_EVENT_READ = 2


@dataclass(frozen=True)
class RetainedEvaluation:
    outcome: str
    reason: str
    retained_candidate_count: int
    coverage_revision: int | None = None
    evidence_packet_revision: int | None = None
    temporal_authority: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"sufficient", "insufficient", "blocked"}:
            raise ValueError(f"unsupported retained evaluation outcome: {self.outcome}")
        if self.retained_candidate_count < 0:
            raise ValueError("retained_candidate_count must be non-negative")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "retained-evaluation-v1",
            "outcome": self.outcome,
            "reason": self.reason,
            "retained_candidate_count": self.retained_candidate_count,
            "coverage_revision": self.coverage_revision,
            "evidence_packet_revision": self.evidence_packet_revision,
            "temporal_authority": self.temporal_authority,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RetainedEvaluation:
        if payload.get("schema_version") != "retained-evaluation-v1":
            raise ControllerBlockedError("persisted retained evaluation is malformed")
        try:
            count = int(payload.get("retained_candidate_count", 0))
        except (TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "persisted retained evaluation candidate count is malformed"
            ) from exc
        return cls(
            outcome=str(payload.get("outcome") or ""),
            reason=bounded_text(payload.get("reason")),
            retained_candidate_count=count,
            coverage_revision=_optional_int(payload.get("coverage_revision")),
            evidence_packet_revision=_optional_int(
                payload.get("evidence_packet_revision")
            ),
            temporal_authority=(
                str(payload["temporal_authority"])
                if payload.get("temporal_authority")
                else None
            ),
        )


class RetainedReviewService:
    """Select, bind, and evaluate retained evidence before acquisition admission."""

    def __init__(
        self,
        *,
        config: Any,
        run_service: ResearchRunService,
        corpus_service: Any,
        coverage_service: CoverageService,
        evidence_service: Any,
        semantic_service: SemanticCallService,
        controller_config: ControllerConfig,
    ) -> None:
        self.config = config
        self.run_service = run_service
        self.corpus_service = corpus_service
        self.coverage_service = coverage_service
        self.evidence_service = evidence_service
        self.semantic_service = semantic_service
        self.controller_config = controller_config

    def evaluate(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
        *,
        evaluated_at: Any,
    ) -> RetainedEvaluation:
        existing = self.load_evaluation(status.id)
        if existing is not None:
            return existing

        selection = self.load_selection(status.id)
        if selection is None:
            try:
                selection = self._select(status, bundle)
            except (ControllerBlockedError, KeyError, TypeError, ValueError) as exc:
                evaluation = RetainedEvaluation(
                    "blocked",
                    bounded_text(exc),
                    0,
                )
                self._record_evaluation(status, evaluation)
                return evaluation

        if not selection:
            evaluation = RetainedEvaluation(
                "insufficient",
                "authoritative retained corpus returned no bounded candidates",
                0,
            )
            self._record_evaluation(status, evaluation)
            return evaluation

        temporal_insufficiency = self._temporal_precheck(
            status,
            bundle,
            selection,
            evaluated_at=evaluated_at,
        )
        if temporal_insufficiency is not None:
            self._record_evaluation(status, temporal_insufficiency)
            return temporal_insufficiency

        try:
            evaluation = self._prepare_evidence(status, bundle, selection)
        except TemporalCoverageUnsatisfied:
            evaluation = RetainedEvaluation(
                "blocked",
                (
                    "retained temporal qualification diverged from the persisted "
                    "controller evaluation clock"
                ),
                len(selection),
                temporal_authority="publication_or_explicit_update",
            )
        except (EvidencePreparationError, KeyError, TypeError, ValueError) as exc:
            evaluation = RetainedEvaluation(
                "blocked",
                bounded_text(exc),
                len(selection),
            )

        self._record_evaluation(status, evaluation)
        return evaluation

    def ensure_selection(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
    ) -> list[dict[str, str]]:
        """Persist retained candidates without preparing evidence yet."""
        selection = self.load_selection(status.id)
        return selection if selection is not None else self._select(status, bundle)

    def evaluate_curated(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
        *,
        evaluated_at: Any,
    ) -> RetainedEvaluation:
        """Evaluate only the retained subset surviving a completed curation action."""
        existing = self.load_evaluation(status.id)
        if existing is not None:
            return existing
        selection = self.ensure_selection(status, bundle)
        retained_snapshots = self._curated_retained_snapshot_ids(status.id)
        curated = [
            item for item in selection if item["snapshot_id"] in retained_snapshots
        ]
        if not curated:
            evaluation = RetainedEvaluation(
                "insufficient",
                "curated retained selection contains no retained evidence",
                0,
            )
            self._record_evaluation(status, evaluation)
            return evaluation
        temporal_insufficiency = self._temporal_precheck(
            status,
            bundle,
            curated,
            evaluated_at=evaluated_at,
        )
        if temporal_insufficiency is not None:
            self._record_evaluation(status, temporal_insufficiency)
            return temporal_insufficiency
        try:
            evaluation = self._prepare_evidence(status, bundle, curated)
        except TemporalCoverageUnsatisfied:
            evaluation = RetainedEvaluation(
                "blocked",
                (
                    "curated retained temporal qualification diverged from the "
                    "persisted controller evaluation clock"
                ),
                len(curated),
                temporal_authority="publication_or_explicit_update",
            )
        except (EvidencePreparationError, KeyError, TypeError, ValueError) as exc:
            evaluation = RetainedEvaluation(
                "blocked",
                bounded_text(exc),
                len(curated),
            )
        self._record_evaluation(status, evaluation)
        return evaluation

    def _curated_retained_snapshot_ids(self, run_id: UUID) -> set[str]:
        with self.run_service.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT snapshot_id FROM run_asset_promotion_subjects
                   WHERE run_id=%s AND current_stage='retained'""",
                (run_id,),
            )
            return {str(row[0]) for row in cursor.fetchall()}

    def load_evaluation(self, run_id: UUID) -> RetainedEvaluation | None:
        event = self._single_event(run_id, _RETAINED_EVALUATION_EVENT)
        if event is None:
            return None
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ControllerBlockedError("persisted retained evaluation is malformed")
        try:
            return RetainedEvaluation.from_payload(payload)
        except ValueError as exc:
            raise ControllerBlockedError(
                "persisted retained evaluation is malformed"
            ) from exc

    def load_selection(self, run_id: UUID) -> list[dict[str, str]] | None:
        event = self._single_event(run_id, _RETAINED_SELECTION_EVENT)
        if event is None:
            return None
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ControllerBlockedError("persisted retained selection is malformed")
        if payload.get("schema_version") != "retained-selection-v1":
            raise ControllerBlockedError("persisted retained selection is malformed")
        selection = payload.get("selection")
        if not isinstance(selection, list):
            raise ControllerBlockedError("persisted retained selection is malformed")

        normalized: list[dict[str, str]] = []
        for raw_item in selection:
            if not isinstance(raw_item, Mapping):
                raise ControllerBlockedError(
                    "persisted retained selection contains a malformed item"
                )
            chunk_id = str(raw_item.get("chunk_id") or "")
            snapshot_id = str(raw_item.get("snapshot_id") or "")
            url = str(raw_item.get("url") or "")
            try:
                UUID(chunk_id)
                UUID(snapshot_id)
            except ValueError as exc:
                raise ControllerBlockedError(
                    "persisted retained selection contains invalid identities"
                ) from exc
            if not url:
                raise ControllerBlockedError(
                    "persisted retained selection contains an empty source URL"
                )
            normalized.append(
                {
                    "chunk_id": chunk_id,
                    "snapshot_id": snapshot_id,
                    "url": url,
                    "query_index": str(raw_item.get("query_index") or "0"),
                    "rank": str(raw_item.get("rank") or "0"),
                }
            )
        return normalized

    def _select(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
    ) -> list[dict[str, str]]:
        effective_caps = bundle.budget.get("effective_caps") or {}
        policy_limit = int(effective_caps.get("max_retrieval_candidates") or 0)
        if policy_limit < 1:
            raise ControllerBlockedError(
                "authoritative budget has no retained-retrieval allowance"
            )
        limit = min(policy_limit, self.controller_config.max_retained_candidates)
        query_texts = _retained_query_texts(bundle.spec)
        if len(query_texts) > limit:
            raise ControllerBlockedError(
                "retained query scope exceeds the authorized candidate budget"
            )
        base_quota, extra = divmod(limit, len(query_texts))
        selected: list[dict[str, str]] = []
        seen_chunks: set[str] = set()

        for query_index, query in enumerate(query_texts):
            query_limit = base_quota + (1 if query_index < extra else 0)
            execution, candidates = self.corpus_service.search_assets(
                query,
                candidate_limit=query_limit,
                run_id=status.id,
                requested_mode="lexical",
            )
            if execution.mechanical_status is MechanicalStatus.FAILED:
                raise ControllerBlockedError(
                    "PostgreSQL retained-corpus retrieval failed mechanically"
                )
            for rank, candidate in enumerate(candidates, 1):
                if rank > query_limit:
                    break
                item = _selection_item(candidate, query_index=query_index, rank=rank)
                if item["chunk_id"] in seen_chunks:
                    continue
                seen_chunks.add(item["chunk_id"])
                selected.append(item)

        key = f"controller:retained-selection:{status.id}:spec{bundle.spec_revision}"
        payload = {
            "schema_version": "retained-selection-v1",
            "selection": selected,
            "candidate_limit": limit,
            "retriever": "postgres_fts",
            "qdrant_authoritative": False,
        }
        external_id = status.external_id
        if external_id is None:
            raise ControllerBlockedError("run is missing public external identity")

        with self.run_service.uow_factory() as uow:
            for item in selected:
                uow.snapshots.link_run_asset(
                    external_id,
                    UUID(item["snapshot_id"]),
                    role="retained",
                    metadata={
                        "controller": "research-controller-v1",
                        "selection_key": key,
                    },
                )
            uow.runs.append_event(
                status.id,
                _RETAINED_SELECTION_EVENT,
                "controller",
                key,
                actor_identifier="ResearchWorkflowController",
                payload=payload,
            )
            uow.commit()
        return selected

    def _temporal_precheck(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
        selection: list[dict[str, str]],
        *,
        evaluated_at: Any,
    ) -> RetainedEvaluation | None:
        spec = serialize_model(bundle.spec)
        if not has_temporal_obligations(spec):
            return None
        if getattr(evaluated_at, "tzinfo", None) is None:
            raise ControllerBlockedError(
                "persisted controller evaluation clock is timezone-naive"
            )
        chunk_ids = [UUID(item["chunk_id"]) for item in selection]
        _execution, passages = self.corpus_service.select_run_passages(
            status.id,
            chunk_ids,
            max_tokens=3000,
            max_passages=min(20, len(chunk_ids)),
        )
        qualifying = [
            passage
            for passage in passages
            if passage_temporally_qualifies(
                passage,
                spec,
                now=evaluated_at,
            )
        ]
        if qualifying:
            return None
        return RetainedEvaluation(
            "insufficient",
            (
                "retained evidence does not satisfy temporal obligations at "
                "the persisted controller evaluation clock"
            ),
            len(selection),
            temporal_authority="publication_or_explicit_update",
        )

    def _prepare_evidence(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
        selection: list[dict[str, str]],
    ) -> RetainedEvaluation:
        ledger = self.coverage_service.rebuild_projection(status.id)
        coverage_revision = int(ledger.revision)
        self.coverage_service.create_snapshot(
            status.id,
            serialize_model(ledger),
            coverage_revision=coverage_revision,
            idempotency_key=(
                f"controller:retained-packet-coverage:{status.id}:r{coverage_revision}"
            ),
        )
        coverage_items = _coverage_items(bundle.spec, ledger)
        extracted_assets = [
            {
                "status": "complete",
                "requested_url": item["url"],
                "snapshot_id": item["snapshot_id"],
                "chunk_ids": [item["chunk_id"]],
                # Retained chunks do not have Firecrawl search-candidate rows.
                # The immutable chunk UUID is the deterministic packet identity.
                "candidate_id": item["chunk_id"],
                "retained": True,
            }
            for item in selection
        ]
        preparation = EvidencePreparationService(
            corpus_service=self.corpus_service,
            evidence_service=self.evidence_service,
            coverage_service=self.coverage_service,
            semantic_service=self.semantic_service,
            config=self.config,
        )
        prepared = preparation.prepare(
            run_id=status.id,
            run_revision=status.lifecycle_revision,
            spec=serialize_model(bundle.spec),
            research_spec_id=UUID(str(bundle.spec.research_spec_id)),
            coverage_revision=coverage_revision,
            extracted_assets=extracted_assets,
            coverage_items=coverage_items,
        )
        updated = self.coverage_service.rebuild_projection(status.id)
        sufficient = getattr(updated.overall_status, "value", "") == "sufficient"
        return RetainedEvaluation(
            "sufficient" if sufficient else "insufficient",
            (
                "retained evidence satisfied authoritative coverage"
                if sufficient
                else (
                    "retained evidence remained insufficient after "
                    "authoritative evaluation"
                )
            ),
            len(selection),
            coverage_revision=updated.revision,
            evidence_packet_revision=prepared.packet_revision,
            temporal_authority="publication_or_explicit_update",
        )

    def _record_evaluation(
        self,
        status: RunStatus,
        evaluation: RetainedEvaluation,
    ) -> None:
        payload = evaluation.to_payload()
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        with self.run_service.uow_factory() as uow:
            uow.runs.append_event(
                status.id,
                _RETAINED_EVALUATION_EVENT,
                "controller",
                f"controller:retained-evaluation:{status.id}:{digest}",
                actor_identifier="ResearchWorkflowController",
                payload=payload,
            )
            uow.commit()

    def _single_event(self, run_id: UUID, event_type: str) -> dict[str, Any] | None:
        with self.run_service.uow_factory() as uow:
            events = uow.runs.list_events(
                run_id,
                event_type=event_type,
                limit=_MAX_EVENT_READ,
                offset=0,
            )
        if not events:
            return None
        if len(events) > 1:
            raise ControllerBlockedError(
                f"multiple authoritative {event_type} events exist for one run"
            )
        return events[0]


def _retained_query_texts(spec: ResearchSpec) -> tuple[str, ...]:
    texts = [question.text for question in spec.questions]
    texts.extend(claim.statement for claim in spec.claims_to_validate)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in texts:
        text = " ".join(str(value).split())
        marker = text.casefold()
        if text and marker not in seen:
            seen.add(marker)
            normalized.append(text)
    if not normalized:
        normalized.append(spec.objective)
    return tuple(normalized)


def _selection_item(
    candidate: Mapping[str, Any],
    *,
    query_index: int,
    rank: int,
) -> dict[str, str]:
    chunk_id = str(candidate.get("candidate_id") or "")
    snapshot_id = str(candidate.get("snapshot_id") or "")
    url = str(candidate.get("url") or "")
    UUID(chunk_id)
    UUID(snapshot_id)
    if not url:
        raise ValueError("retained candidate is missing its authoritative source URL")
    return {
        "chunk_id": chunk_id,
        "snapshot_id": snapshot_id,
        "url": url,
        "query_index": str(query_index),
        "rank": str(rank),
    }


def _coverage_items(spec: ResearchSpec, ledger: Any) -> list[dict[str, Any]]:
    text_by_subject: dict[str, str] = {
        str(item.question_id): item.text for item in spec.questions
    }
    text_by_subject.update(
        {str(item.claim_id): item.statement for item in spec.claims_to_validate}
    )
    text_by_subject.update(
        {
            str(item.requirement_id): item.description
            for item in spec.freshness_requirements
        }
    )
    text_by_subject.update(
        {
            str(item.requirement_id): item.source_class
            for item in spec.required_source_classes
        }
    )
    text_by_subject.update(
        {
            str(item.requirement_id): item.description
            for item in spec.corroboration_requirements
        }
    )
    text_by_subject.update(
        {
            str(item.requirement_id): item.description
            for item in spec.contradiction_requirements
        }
    )
    return [
        {
            "coverage_item_id": str(item.coverage_item_id),
            "item_type": getattr(item.item_type, "value", str(item.item_type)),
            "subject_id": str(item.subject_id),
            "text": text_by_subject.get(
                str(item.subject_id),
                str(item.remaining_gap or ""),
            ),
            "remaining_gap": str(item.remaining_gap or ""),
        }
        for item in ledger.items
    ]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ControllerBlockedError("persisted retained integer is malformed") from exc


__all__ = ["RetainedEvaluation", "RetainedReviewService"]
