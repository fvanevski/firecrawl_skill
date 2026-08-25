"""Canonical #311 planned-acquisition authority boundary.

Semantic query fields never select provider operations. Planned acquisition uses
the persisted PostgreSQL budget snapshot plus durable extraction/search state to
compute the remaining deterministic candidate-selection allowance. Low-level
search services remain available for specialist use, but production controller
composition routes through the classes in this module.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from .acquisition.models import AcquisitionResult
from .acquisition.temporal_acquisition import TemporalAcquisitionService
from .bounded_orchestrator import (
    BoundedAcquisitionStage,
    _apply_preflight_metadata,
    _audit_message,
    _safe_int,
)
from .budget_policy import ResourceCaps
from .domain import IngestRequest, SearchAdapterResult
from .orchestrator import _minimum_authoritative_source_target
from .provider_preflight import CandidatePreflightResult, validate_candidate_url
from .recency import normalize_recency_window
from .run_service import RunStateError, StaleRunRevisionError
from .stages import ContextKeys, StageResult

logger = logging.getLogger(__name__)

_MAX_PERSISTED_EXTRACTION_ATTEMPTS = 1000


@dataclass(frozen=True)
class PlannedAcquisitionAuthority:
    """One immutable read of the persisted resource/progress authority."""

    caps: ResourceCaps
    attempted: int
    succeeded: int
    executed_query_texts: frozenset[str]


def _persisted_resource_caps(context: Mapping[str, Any]) -> ResourceCaps:
    budget = context.get("authoritative_budget")
    if not isinstance(budget, Mapping):
        # Persisted controller context validation uses the established ValueError contract.
        raise ValueError(  # noqa: TRY004
            "planned acquisition requires persisted authoritative_budget"
        )
    raw_caps = budget.get("effective_caps")
    if not isinstance(raw_caps, Mapping):
        raise ValueError(  # noqa: TRY004
            "persisted authoritative_budget has no effective_caps"
        )
    return ResourceCaps.from_mapping(dict(raw_caps))


def load_planned_acquisition_authority(
    run_service: Any,
    run_id: UUID,
    context: Mapping[str, Any],
) -> PlannedAcquisitionAuthority:
    """Load budget and restart counters only from persisted run authority."""

    caps = _persisted_resource_caps(context)
    with run_service.uow_factory() as uow:
        attempted = int(uow.extraction_attempts.count_for_run(run_id))
        if attempted > _MAX_PERSISTED_EXTRACTION_ATTEMPTS:
            raise ValueError(
                "run extraction-attempt census exceeds the supported bounded size"
            )
        attempts = uow.extraction_attempts.list_attempts_for_run(
            run_id,
            limit=_MAX_PERSISTED_EXTRACTION_ATTEMPTS,
            offset=0,
        )
        if len(attempts) != attempted:
            raise ValueError(
                "run extraction-attempt census changed during authoritative read"
            )
        succeeded = sum(
            1
            for attempt in attempts
            if str(attempt.get("exit_status") or "") == "succeeded"
        )
        responses = uow.search_responses.list_search_responses(run_id)
    if attempted > caps.max_extraction_attempts:
        raise ValueError(
            "persisted extraction attempts exceed authoritative budget snapshot"
        )
    if succeeded > caps.max_successful_extractions:
        raise ValueError(
            "persisted successful extractions exceed authoritative budget snapshot"
        )
    executed = frozenset(
        text for row in responses if (text := str(row.get("query_text") or "").strip())
    )
    return PlannedAcquisitionAuthority(
        caps=caps,
        attempted=attempted,
        succeeded=succeeded,
        executed_query_texts=executed,
    )


class DeterministicPlannedTemporalAcquisitionService(TemporalAcquisitionService):
    """Temporal acquisition with a selection cap distinct from provider volume."""

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
        selection_limit: int | None = None,
        sources: str = "web",
        tbs: str | None = None,
        metadata: dict[str, Any] | None = None,
        authority_context: Any | None = None,
        replay_existing: bool = True,
    ) -> AcquisitionResult:
        if selection_limit is None:
            effective_selection_limit = limit
        elif (
            isinstance(selection_limit, bool)
            or not isinstance(selection_limit, int)
            or selection_limit < 0
        ):
            raise ValueError("selection_limit must be a non-negative integer")
        else:
            effective_selection_limit = selection_limit

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
        persisted_metadata["candidate_selection_limit"] = effective_selection_limit
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
            max_selected=effective_selection_limit,
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


class DeterministicPlannedAcquisitionStage(BoundedAcquisitionStage):
    """Production acquisition stage for a persisted deterministic SearchPlan."""

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
        if not isinstance(search_plan, Mapping):
            return StageResult.failed(
                "acquisition", "Search plan not available for acquisition"
            )
        raw_queries = search_plan.get("queries")
        if not isinstance(raw_queries, list) or not raw_queries:
            return StageResult.failed(
                "acquisition", "Search plan has no queries to execute"
            )
        if not isinstance(
            self.acquisition_service, DeterministicPlannedTemporalAcquisitionService
        ):
            return StageResult.failed(
                "acquisition",
                "planned acquisition requires deterministic temporal acquisition service",
            )

        try:
            authority = load_planned_acquisition_authority(
                self.run_service,
                run_id,
                context,
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "acquisition",
                f"could not load persisted acquisition authority: {exc}",
            )
        caps = authority.caps
        context["effective_resource_caps"] = caps.to_dict()

        queries = [dict(query) for query in raw_queries if isinstance(query, Mapping)]
        plan_query_ids = {
            str(query.get("query_id"))
            for query in queries
            if query.get("query_id") is not None
        }
        authorized_proposals = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
        if isinstance(authorized_proposals, list):
            existing_texts = {str(query.get("query") or "") for query in queries}
            for proposal in authorized_proposals:
                if not isinstance(proposal, Mapping):
                    continue
                proposed_queries = proposal.get("proposed_queries", [])
                if not isinstance(proposed_queries, list):
                    continue
                for query in proposed_queries:
                    if not isinstance(query, Mapping):
                        continue
                    query_text = str(query.get("query") or "").strip()
                    if query_text and query_text not in existing_texts:
                        queries.append(dict(query))
                        existing_texts.add(query_text)

        def query_order(query: Mapping[str, Any]) -> tuple[Any, ...]:
            query_id = str(query.get("query_id") or "")
            if query_id in plan_query_ids:
                raw_priority = query.get("priority")
                priority = (
                    int(raw_priority)
                    if isinstance(raw_priority, int)
                    and not isinstance(raw_priority, bool)
                    else 2_147_483_647
                )
                return (0, priority, query_id)
            return (
                1,
                str(query.get("query") or "").casefold(),
                str(query.get("facet") or "").casefold(),
            )

        queries.sort(key=query_order)

        response_ids: list[str] = []
        candidate_count = 0
        successful_urls = 0
        candidate_ids: list[str] = []
        raw_ingest_requests: list[dict[str, Any]] = []
        candidate_targets: dict[str, list[str]] = context.setdefault(
            "candidate_coverage_items", {}
        )
        scheduled_candidates: set[str] = context.setdefault(
            "scheduled_candidate_ids", set()
        )
        executed_queries: set[str] = context.setdefault("executed_query_texts", set())
        executed_queries.update(authority.executed_query_texts)
        extraction_attempt_count = max(
            int(context.get("extraction_attempt_count", 0)),
            authority.attempted,
        )
        successful_extraction_count = max(
            int(context.get("successful_extraction_count", 0)),
            authority.succeeded,
        )
        source_target = min(
            caps.max_successful_extractions,
            _minimum_authoritative_source_target(context["spec"]),
        )
        coverage_by_subject = {
            str(item["subject_id"]): str(item["coverage_item_id"])
            for item in context.get("coverage_items", [])
            if isinstance(item, Mapping) and item.get("subject_id") is not None
        }
        persisted_plan_id = context.get("search_plan_id")
        plan_id = (
            UUID(str(persisted_plan_id)) if persisted_plan_id is not None else None
        )

        for query in queries:
            query_text = str(query.get("query") or "").strip()
            if not query_text or query_text in executed_queries:
                continue
            if len(executed_queries) >= caps.max_search_branches:
                break
            remaining_attempts = max(
                0,
                caps.max_extraction_attempts - extraction_attempt_count,
            )
            remaining_successes = max(0, source_target - successful_extraction_count)
            if remaining_attempts == 0 or remaining_successes == 0:
                break
            selection_limit = min(caps.results_per_branch, remaining_attempts)

            query_id = str(query.get("query_id") or "")
            plan_query_id = (
                UUID(query_id)
                if plan_id is not None and query_id in plan_query_ids
                else None
            )
            try:
                # Search is discovery-only. Semantic facet/purpose is metadata and
                # can never select a provider scrape operation.
                result = self.acquisition_service.execute_search(
                    run_id,
                    query_text,
                    backend="firecrawl",
                    plan_id=plan_id if plan_query_id is not None else None,
                    plan_query_id=plan_query_id,
                    idempotency_key=(
                        f"acquire:{run_id}:{query_id or query_text}:"
                        f"selection:{selection_limit}"
                    ),
                    limit=caps.results_per_branch,
                    selection_limit=selection_limit,
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
                        if isinstance(item, Mapping)
                        and item.get("item_type") == "source_requirement"
                    )
                if query.get("freshness_requirement"):
                    query_targets.extend(
                        str(item["coverage_item_id"])
                        for item in context.get("coverage_items", [])
                        if isinstance(item, Mapping)
                        and item.get("item_type") == "freshness_requirement"
                    )
                query_targets = list(dict.fromkeys(query_targets))

                for cand in result.candidates:
                    cid = cand.get("candidate_id") or cand.get("id")
                    if not cid:
                        continue
                    cid_str = str(cid)
                    candidate_ids.append(cid_str)
                    existing_targets = candidate_targets.setdefault(cid_str, [])
                    for target in query_targets:
                        if target not in existing_targets:
                            existing_targets.append(target)
                    if cid_str in scheduled_candidates:
                        continue
                    if extraction_attempt_count >= caps.max_extraction_attempts:
                        break
                    scheduled_candidates.add(cid_str)

                    raw_item = cand.get("raw_item") or {}
                    provider_metadata = (
                        raw_item.get("metadata")
                        if isinstance(raw_item, Mapping)
                        else {}
                    ) or {}
                    url = (
                        cand.get("canonical_url")
                        or cand.get("original_url")
                        or provider_metadata.get("sourceURL")
                        or provider_metadata.get("url")
                    )
                    request_metadata: dict[str, Any] = {
                        "candidate_id": cid_str,
                        "candidate_occurrence_id": str(cand.get("id")),
                        "search_response_id": str(result.search_response_id),
                        "firecrawl": {
                            "result_index": len(raw_ingest_requests),
                            "scrape_id": provider_metadata.get("scrapeId"),
                            "source_url": provider_metadata.get("sourceURL") or url,
                            "status_code": provider_metadata.get("statusCode"),
                        },
                    }

                    preflight: CandidatePreflightResult | None = None
                    raw_preflight = provider_metadata.get("_preflight")
                    if isinstance(raw_preflight, Mapping):
                        preflight = CandidatePreflightResult.from_metadata(
                            raw_preflight
                        )
                    else:
                        preflight = validate_candidate_url(str(url or ""))

                    markdown = (
                        raw_item.get("markdown")
                        if isinstance(raw_item, Mapping)
                        else None
                    )
                    if preflight is None and isinstance(markdown, str):
                        synthetic = SearchAdapterResult(
                            raw_payload=(
                                b'{"markdown": ""}'
                                if not markdown
                                else (
                                    b'{"markdown": '
                                    + json.dumps(markdown).encode()
                                    + b"}"
                                )
                            ),
                            http_status=_safe_int(provider_metadata.get("statusCode")),
                            transport_metadata={
                                "content_type": provider_metadata.get("contentType")
                                or provider_metadata.get("content_type")
                            },
                        )
                        preflight = self.preflight_checker.check(synthetic)

                    if preflight is not None:
                        _apply_preflight_metadata(request_metadata, preflight)

                    item: dict[str, Any] = {
                        "requested_url": str(url or "unknown:"),
                        "title": cand.get("title"),
                        "metadata": request_metadata,
                    }
                    if (
                        isinstance(markdown, str)
                        and markdown.strip()
                        and preflight is not None
                        and not preflight.terminal
                    ):
                        item["request"] = IngestRequest(
                            requested_url=str(url),
                            final_url=provider_metadata.get("url")
                            or provider_metadata.get("sourceURL")
                            or str(url),
                            content=markdown.encode("utf-8"),
                            normalized_content=markdown.encode("utf-8"),
                            mime_type="text/markdown",
                            title=cand.get("title"),
                            http_status=_safe_int(provider_metadata.get("statusCode")),
                            firecrawl_version="cli-1.19.27",
                            crawl_options={
                                "operation": "bounded candidate scrape",
                                "formats": ["markdown"],
                            },
                            metadata=request_metadata,
                        )
                        successful_urls += 1
                    elif preflight is not None and preflight.terminal:
                        item["error"] = _audit_message(preflight)

                    raw_ingest_requests.append(item)
                    extraction_attempt_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("acquisition query failed: %s — %s", query_text, exc)

        if coverage_revision is not None:
            try:
                wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
                for index, cand_id in enumerate(candidate_ids):
                    for item_id in candidate_targets.get(cand_id, []):
                        self.coverage_service.apply_candidate_identified(
                            run_id,
                            UUID(item_id),
                            candidate_id=UUID(cand_id),
                            idempotency_key=(
                                f"acquire:cand:{run_id}:w{wave_count}:{index}:{item_id}"
                            ),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("coverage update after acquisition failed: %s", exc)

        context[ContextKeys.SEARCH_RESPONSE_IDS] = response_ids
        context[ContextKeys.CANDIDATE_COUNT] = candidate_count
        context[ContextKeys.SUCCESSFUL_URLS] = successful_urls
        context["raw_ingest_requests"] = raw_ingest_requests
        context["extraction_attempt_count"] = extraction_attempt_count
        context["successful_extraction_count"] = successful_extraction_count

        if raw_ingest_requests:
            try:
                self.run_service.transition(
                    run_id,
                    "extracting",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:acquisition_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="DeterministicPlannedAcquisitionStage",
                    triggering_event="run.extracting",
                    reason=(
                        f"acquired {candidate_count} candidates for bounded extraction"
                    ),
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("acquisition", str(exc))
            return StageResult.ok(
                "acquisition",
                f"executed {len(response_ids)} new queries, {candidate_count} candidates",
                details={
                    ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                    ContextKeys.CANDIDATE_COUNT: candidate_count,
                    ContextKeys.SUCCESSFUL_URLS: successful_urls,
                },
            )

        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision,
                idempotency_key=f"stage:acquisition_empty:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="DeterministicPlannedAcquisitionStage",
                triggering_event="run.coverage_review",
                reason="no candidates admitted within persisted acquisition budget",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("acquisition", str(exc))
        return StageResult.ok(
            "acquisition",
            f"executed {len(response_ids)} new queries, 0 candidates (empty)",
            details={
                ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                ContextKeys.CANDIDATE_COUNT: 0,
                ContextKeys.SUCCESSFUL_URLS: 0,
            },
        )


__all__ = [
    "DeterministicPlannedAcquisitionStage",
    "DeterministicPlannedTemporalAcquisitionService",
    "PlannedAcquisitionAuthority",
    "load_planned_acquisition_authority",
]
