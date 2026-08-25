"""Exact-recency, temporal-admission, and deterministic candidate-selection wrapper.

Provider recency is discovery-only. PostgreSQL retains the exact discovery
window and the ResearchSpec remains the evidence authority. Candidate temporal
normalization and deterministic pre-scrape admission are performed from
persisted PostgreSQL state; generic provider dates are never publication proof.

After temporal admission, model assistance is limited to semantic labels.
Application policy deterministically performs exclusions, ranking, diversity
and bounded selection, then persists the response-scoped decision for replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain import load_model
from firecrawl_skill.research_domain.models import ResearchSpec

from ..candidate_selection_policy import (
    CANDIDATE_SELECTION_SCHEMA_VERSION,
    fallback_candidate_labels,
    select_candidates,
    selection_fingerprint,
    semantic_candidate_labels,
)
from ..candidate_temporal_policy import assess_candidate_temporal
from ..plan_recency import plan_query_recency_tbs
from ..recency import RecencyWindow, normalize_recency_window
from ..semantic_service import SemanticCallService, redact_sensitive
from ..temporal_candidate import ranking_safe_raw_item
from .models import AcquisitionResult
from .service import AcquisitionIdempotencyConflictError


class TemporalAcquisitionService:
    """Keep discovery recency and evidence temporal semantics distinct."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.uow_factory = delegate.uow_factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @property
    def idempotency_lock_timeout_seconds(self) -> float:
        return float(self.delegate.idempotency_lock_timeout_seconds)

    @idempotency_lock_timeout_seconds.setter
    def idempotency_lock_timeout_seconds(self, value: float) -> None:
        self.delegate.idempotency_lock_timeout_seconds = value

    @property
    def idempotency_lock_poll_seconds(self) -> float:
        return float(self.delegate.idempotency_lock_poll_seconds)

    @idempotency_lock_poll_seconds.setter
    def idempotency_lock_poll_seconds(self, value: float) -> None:
        self.delegate.idempotency_lock_poll_seconds = value

    @staticmethod
    def _default_key(
        run_id: UUID,
        query_text: str,
        *,
        plan_query_id: UUID | None,
        limit: int,
        sources: str,
        tbs: str | None,
    ) -> str:
        payload = {
            "query_text": query_text,
            "plan_query_id": str(plan_query_id) if plan_query_id else None,
            "limit": int(limit),
            "sources": sources,
            "tbs": tbs,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"search:{run_id}:{digest}"

    def execute_search(
        self,
        run_id: UUID,
        query_text: str,
        *,
        backend: str = "firecrawl",
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        parent_invocation_id: UUID | None = None,
        idempotency_key: str | None = None,
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        metadata: dict[str, Any] | None = None,
        authority_context: Any | None = None,
        replay_existing: bool = True,
    ) -> AcquisitionResult:
        run_uuid = UUID(str(run_id))
        if tbs is None:
            tbs = self._persisted_plan_recency(
                run_uuid,
                query_text,
                plan_query_id=plan_query_id,
            )
        window = normalize_recency_window(tbs)
        provider_tbs = window.provider_tbs if window is not None else None
        persisted_metadata = dict(metadata or {})
        if window is not None:
            persisted_metadata["recency"] = window.to_dict()
        key = idempotency_key or self._default_key(
            run_uuid,
            query_text,
            plan_query_id=(
                UUID(str(plan_query_id)) if plan_query_id is not None else None
            ),
            limit=limit,
            sources=sources,
            tbs=tbs,
        )
        self._assert_exact_replay_semantics(run_uuid, key, window)
        result = self.delegate.execute_search(
            run_uuid,
            query_text,
            backend=backend,
            plan_id=plan_id,
            plan_query_id=plan_query_id,
            parent_invocation_id=parent_invocation_id,
            idempotency_key=key,
            limit=limit,
            sources=sources,
            tbs=provider_tbs,
            metadata=persisted_metadata,
            authority_context=authority_context,
            replay_existing=replay_existing,
        )
        candidates, admission = self._temporally_admitted_occurrences(
            run_uuid,
            result.search_response_id,
            result.candidates,
            now=self._persisted_response_reference(result),
        )
        candidates, selection = self._deterministically_selected_occurrences(
            run_uuid,
            result.search_response_id,
            candidates,
            max_selected=limit,
        )
        response = dict(result.search_response)
        if window is not None:
            response["recency"] = window.to_dict()
        if admission is not None:
            response["temporal_admission"] = admission
        if selection is not None:
            response["candidate_selection"] = selection
        return replace(
            result,
            candidates=candidates,
            candidate_count=len(candidates),
            search_response=response,
        )

    def _persisted_plan_recency(
        self,
        run_id: UUID,
        query_text: str,
        *,
        plan_query_id: UUID | None,
    ) -> str | None:
        with self.uow_factory() as uow:
            if plan_query_id is not None:
                row = uow.search_responses.get_plan_query(
                    UUID(str(plan_query_id)),
                    run_id=run_id,
                )
            else:
                try:
                    plan = uow.search_responses.get_search_plan(run_id)
                except (KeyError, ValueError):
                    return None
                matches = [
                    item
                    for item in plan.get("queries", ())
                    if item.get("query_text") == query_text
                ]
                if not matches:
                    return None
                if len(matches) != 1:
                    raise ValueError(
                        "persisted search plan contains ambiguous query text"
                    )
                row = matches[0]
        return plan_query_recency_tbs(
            {"freshness_requirement": row.get("freshness_requirement")}
        )

    @staticmethod
    def _persisted_response_reference(result: AcquisitionResult) -> datetime | None:
        """Return the persisted search-response timestamp as the evaluation reference."""

        response = result.search_response
        reference = (
            response.get("responded_at") if isinstance(response, Mapping) else None
        )
        return reference if isinstance(reference, datetime) else None

    @staticmethod
    def _persisted_admission_snapshot(
        uow: Any,
        run_id: UUID,
        search_response_id: UUID,
    ) -> dict[str, Any] | None:
        key = f"temporal-admission:{search_response_id}"
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT payload FROM research_events
                     WHERE run_id=%s AND idempotency_key=%s
                       AND event_type='acquisition.temporal_admission'""",
                (run_id, key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        payload = row[0]
        if not isinstance(payload, Mapping):
            raise AcquisitionIdempotencyConflictError(
                "persisted temporal admission event has malformed payload"
            )
        if str(payload.get("search_response_id") or "") != str(search_response_id):
            raise AcquisitionIdempotencyConflictError(
                "persisted temporal admission event targets another response"
            )
        return dict(payload)

    @staticmethod
    def _admitted_from_snapshot(
        occurrences: list[dict[str, Any]],
        snapshot: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        summary = snapshot.get("summary")
        assessments = snapshot.get("assessments")
        if not isinstance(summary, Mapping) or not isinstance(assessments, list):
            raise AcquisitionIdempotencyConflictError(
                "persisted temporal admission snapshot is incomplete"
            )

        by_candidate: dict[str, dict[str, Any]] = {}
        for value in assessments:
            if not isinstance(value, Mapping):
                raise AcquisitionIdempotencyConflictError(
                    "persisted temporal admission assessment is malformed"
                )
            candidate_id = str(value.get("candidate_id") or "")
            status = value.get("status")
            if not candidate_id or status not in {"eligible", "unknown", "ineligible"}:
                raise AcquisitionIdempotencyConflictError(
                    "persisted temporal admission assessment is invalid"
                )
            normalized = dict(value)
            previous = by_candidate.get(candidate_id)
            if previous is not None and previous != normalized:
                raise AcquisitionIdempotencyConflictError(
                    "persisted temporal admission has conflicting candidate assessments"
                )
            by_candidate[candidate_id] = normalized

        admitted: list[dict[str, Any]] = []
        for occurrence in occurrences:
            candidate_value = occurrence.get("candidate_id") or occurrence.get("id")
            if candidate_value is None:
                continue
            candidate_id = str(candidate_value)
            stored = by_candidate.get(candidate_id)
            if stored is None:
                raise AcquisitionIdempotencyConflictError(
                    "persisted temporal admission is missing a response candidate"
                )
            assessment = {
                key: value for key, value in stored.items() if key != "candidate_id"
            }
            if assessment["status"] == "ineligible":
                continue
            raw = occurrence.get("raw_item") or {}
            safe = ranking_safe_raw_item(raw) if isinstance(raw, Mapping) else {}
            admitted.append(
                {
                    **occurrence,
                    "raw_item": safe,
                    "temporal_assessment": assessment,
                }
            )

        persisted_summary = dict(summary)
        if persisted_summary.get("discovered") != len(occurrences):
            raise AcquisitionIdempotencyConflictError(
                "persisted temporal admission discovery count changed"
            )
        if persisted_summary.get("admitted") != len(admitted):
            raise AcquisitionIdempotencyConflictError(
                "persisted temporal admission admitted count changed"
            )
        return admitted, persisted_summary

    def _temporally_admitted_occurrences(
        self,
        run_id: UUID,
        search_response_id: UUID,
        occurrences: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        with self.uow_factory() as uow:
            spec_row = uow.runs.get_research_spec(run_id)
            if spec_row is None:
                return self._ranking_safe_occurrences(occurrences), None
            snapshot = self._persisted_admission_snapshot(
                uow,
                run_id,
                search_response_id,
            )
            if snapshot is not None:
                return self._admitted_from_snapshot(occurrences, snapshot)

            spec = spec_row.get("payload") or {}
            assessed: list[dict[str, Any]] = []
            admitted: list[dict[str, Any]] = []
            counts = {"eligible": 0, "unknown": 0, "ineligible": 0}
            for occurrence in occurrences:
                candidate_id = occurrence.get("candidate_id") or occurrence.get("id")
                if candidate_id is None:
                    continue
                candidate = uow.candidates.get_candidate(
                    UUID(str(candidate_id)), run_id=run_id
                )
                assessment = assess_candidate_temporal(candidate, spec, now=now)
                assessment_payload = assessment.to_dict()
                counts[assessment.status] += 1
                assessed.append(
                    {
                        "candidate_id": str(candidate_id),
                        **assessment_payload,
                    }
                )
                if assessment.status == "ineligible":
                    continue
                raw = occurrence.get("raw_item") or {}
                safe = ranking_safe_raw_item(raw) if isinstance(raw, Mapping) else {}
                admitted.append(
                    {
                        **occurrence,
                        "raw_item": safe,
                        "temporal_assessment": assessment_payload,
                    }
                )
            summary = {
                "basis": assessed[0]["basis"] if assessed else "none",
                "discovered": len(occurrences),
                "admitted": len(admitted),
                "evaluated_at": now.isoformat() if now is not None else None,
                **counts,
            }
            uow.runs.append_event(
                run_id,
                "acquisition.temporal_admission",
                "system",
                f"temporal-admission:{search_response_id}",
                actor_identifier="TemporalAcquisitionService",
                payload={
                    "search_response_id": str(search_response_id),
                    "summary": summary,
                    "assessments": assessed,
                },
            )
            uow.commit()
        return admitted, summary

    @staticmethod
    def _persisted_selection_snapshot(
        uow: Any,
        run_id: UUID,
        search_response_id: UUID,
    ) -> dict[str, Any] | None:
        key = f"candidate-selection:{search_response_id}"
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT payload FROM research_events
                     WHERE run_id=%s AND idempotency_key=%s
                       AND event_type='acquisition.candidate_selection'""",
                (run_id, key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        payload = row[0]
        if not isinstance(payload, Mapping):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection event has malformed payload"
            )
        if str(payload.get("search_response_id") or "") != str(search_response_id):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection event targets another response"
            )
        if payload.get("schema_version") != CANDIDATE_SELECTION_SCHEMA_VERSION:
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection uses an unsupported schema"
            )
        return dict(payload)

    @staticmethod
    def _selection_from_snapshot(
        candidates: list[dict[str, Any]],
        snapshot: Mapping[str, Any],
        *,
        max_selected: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        decision = snapshot.get("decision")
        labels = snapshot.get("semantic_labels")
        gaps = snapshot.get("coverage_gap_question_ids")
        stored_cap = snapshot.get("max_selected")
        fingerprint = str(snapshot.get("fingerprint") or "")
        if not isinstance(decision, Mapping):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection decision is missing"
            )
        if not isinstance(labels, list) or not all(
            isinstance(item, Mapping) for item in labels
        ):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate semantic labels are malformed"
            )
        if not isinstance(gaps, list):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection coverage gaps are malformed"
            )
        if isinstance(stored_cap, bool) or not isinstance(stored_cap, int):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection resource cap is malformed"
            )
        if stored_cap != max_selected:
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection resource cap changed"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection fingerprint is malformed"
            )
        try:
            recomputed = select_candidates(
                candidates,
                [dict(item) for item in labels],
                max_selected=stored_cap,
                coverage_gap_question_ids=[str(value) for value in gaps],
            )
            recomputed_fingerprint = selection_fingerprint(
                candidates,
                [dict(item) for item in labels],
                recomputed,
                max_selected=stored_cap,
                coverage_gap_question_ids=[str(value) for value in gaps],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionIdempotencyConflictError(
                f"persisted candidate selection cannot be replayed: {exc}"
            ) from exc
        if recomputed.to_dict() != dict(decision):
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection decision contradicts admitted inputs"
            )
        if recomputed_fingerprint != fingerprint:
            raise AcquisitionIdempotencyConflictError(
                "persisted candidate selection fingerprint contradicts admitted inputs"
            )
        return list(recomputed.selected_candidates), {
            "schema_version": CANDIDATE_SELECTION_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "selected_candidate_ids": [
                str(item.get("candidate_id") or item.get("id"))
                for item in recomputed.selected_candidates
            ],
            "semantic_status": str(
                (snapshot.get("semantic_provenance") or {}).get("status") or "persisted"
            ),
            "replayed": True,
        }

    @staticmethod
    def _coverage_gap_question_ids(
        uow: Any,
        run_id: UUID,
        spec: ResearchSpec,
    ) -> tuple[str, ...]:
        """Read current question gaps from immutable PostgreSQL coverage snapshots."""

        known = {str(item.question_id) for item in spec.questions}
        if not known:
            return ()
        coverage = getattr(uow, "coverage", None)
        if coverage is None:
            return tuple(sorted(known))
        current_revision = int(coverage.get_current_revision(run_id))
        snapshot = coverage.get_latest_snapshot(run_id)
        if (
            snapshot is None
            or int(snapshot.get("coverage_revision") or 0) != current_revision
        ):
            return tuple(sorted(known))
        ledger = snapshot.get("ledger") or {}
        if not isinstance(ledger, Mapping):
            raise AcquisitionIdempotencyConflictError(
                "persisted coverage snapshot has malformed ledger"
            )
        items = ledger.get("items") or []
        if not isinstance(items, list):
            raise AcquisitionIdempotencyConflictError(
                "persisted coverage snapshot has malformed item list"
            )
        statuses: dict[str, str] = {}
        for item in items:
            if not isinstance(item, Mapping) or item.get("item_type") != "question":
                continue
            subject = str(item.get("subject_id") or "")
            if subject not in known:
                continue
            status = str(item.get("status") or "unassessed")
            previous = statuses.get(subject)
            if previous is not None and previous != status:
                raise AcquisitionIdempotencyConflictError(
                    f"coverage snapshot is ambiguous for question {subject}"
                )
            statuses[subject] = status
        closed = {"satisfied", "waived"}
        return tuple(
            sorted(
                question_id
                for question_id in known
                if statuses.get(question_id, "unassessed") not in closed
            )
        )

    def _deterministically_selected_occurrences(
        self,
        run_id: UUID,
        search_response_id: UUID,
        candidates: list[dict[str, Any]],
        *,
        max_selected: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if not candidates:
            return [], None

        with self.uow_factory() as uow:
            search_responses = getattr(uow, "search_responses", None)
            if search_responses is None:
                return candidates, None
            try:
                search_responses.get_search_plan(run_id)
            except (KeyError, ValueError):
                # Specialist/unplanned search remains a low-level surface. The
                # canonical planned controller path always has SearchPlan authority.
                return candidates, None
            snapshot = self._persisted_selection_snapshot(
                uow, run_id, search_response_id
            )
            if snapshot is not None:
                return self._selection_from_snapshot(
                    candidates, snapshot, max_selected=max_selected
                )
            spec_row = uow.runs.get_research_spec(run_id)
            status = uow.runs.get_run_status(run_id=run_id)

        if spec_row is None:
            raise AcquisitionIdempotencyConflictError(
                "planned candidate selection requires persisted ResearchSpec"
            )
        spec_value = load_model(spec_row.get("payload") or {})
        if not isinstance(spec_value, ResearchSpec):
            raise AcquisitionIdempotencyConflictError(
                "planned candidate selection ResearchSpec is malformed"
            )
        with self.uow_factory() as uow:
            coverage_gap_question_ids = self._coverage_gap_question_ids(
                uow,
                run_id,
                spec_value,
            )

        semantic = SemanticCallService(
            self.uow_factory,
            host_artifact_supplier=getattr(
                getattr(self.delegate, "config", None),
                "host_artifact_supplier",
                None,
            ),
        )
        context = {
            "run_id": str(run_id),
            "run_revision": int(status["lifecycle_revision"]),
            "stage": "candidate_labeling",
            "schema_name": "candidate-semantic-labels-v1",
            "schema_version": 1,
            "artifact_type": "candidate_semantic_labels",
            "idempotency_key": f"candidate-labels:{search_response_id}",
            "input_artifact_ids": [str(search_response_id)],
        }
        try:
            labels, semantic_provenance = semantic_candidate_labels(
                candidates=candidates,
                spec=spec_value,
                semantic_service=semantic,
                semantic_context=context,
            )
        except Exception as exc:  # noqa: BLE001
            labels = fallback_candidate_labels(candidates)
            semantic_provenance = {
                "status": "degraded",
                "schema_version": "candidate-semantic-labels-v1",
                "error": str(redact_sensitive(f"{type(exc).__name__}: {exc}"))[:1000],
            }

        selection = select_candidates(
            candidates,
            labels,
            max_selected=max_selected,
            coverage_gap_question_ids=coverage_gap_question_ids,
        )
        fingerprint = selection_fingerprint(
            candidates,
            labels,
            selection,
            max_selected=max_selected,
            coverage_gap_question_ids=coverage_gap_question_ids,
        )
        payload = {
            "schema_version": CANDIDATE_SELECTION_SCHEMA_VERSION,
            "search_response_id": str(search_response_id),
            "max_selected": max_selected,
            "coverage_gap_question_ids": list(coverage_gap_question_ids),
            "fingerprint": fingerprint,
            "semantic_provenance": semantic_provenance,
            "semantic_labels": labels,
            "decision": selection.to_dict(),
        }
        with self.uow_factory() as uow:
            raced = self._persisted_selection_snapshot(uow, run_id, search_response_id)
            if raced is not None:
                return self._selection_from_snapshot(
                    candidates, raced, max_selected=max_selected
                )
            uow.runs.append_event(
                run_id,
                "acquisition.candidate_selection",
                "system",
                f"candidate-selection:{search_response_id}",
                actor_identifier="TemporalAcquisitionService",
                payload=payload,
            )
            uow.commit()

        return list(selection.selected_candidates), {
            "schema_version": CANDIDATE_SELECTION_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "selected_candidate_ids": [
                str(item.get("candidate_id") or item.get("id"))
                for item in selection.selected_candidates
            ],
            "semantic_status": str(semantic_provenance.get("status") or ""),
            "replayed": False,
        }

    def _assert_exact_replay_semantics(
        self,
        run_id: UUID,
        idempotency_key: str,
        window: RecencyWindow | None,
    ) -> None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT transport_metadata FROM search_responses
                     WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            row = cursor.fetchone()
        if row is None:
            return
        stored = row[0] or {}
        stored_recency = stored.get("recency") if isinstance(stored, dict) else None
        requested = window.requested_tbs if window is not None else None
        stored_requested = (
            stored_recency.get("requested_tbs")
            if isinstance(stored_recency, dict)
            else None
        )
        if stored_requested != requested:
            raise AcquisitionIdempotencyConflictError(
                "search idempotency key was used with different exact recency semantics"
            )

    @staticmethod
    def _ranking_safe_occurrences(
        occurrences: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for occurrence in occurrences:
            raw = occurrence.get("raw_item") or {}
            safe = ranking_safe_raw_item(raw) if isinstance(raw, Mapping) else {}
            result.append({**occurrence, "raw_item": safe})
        return result

    def reconcile_pending_searches(self, run_id: UUID) -> list[dict[str, Any]]:
        return self.delegate.reconcile_pending_searches(run_id)


__all__ = ["TemporalAcquisitionService"]
