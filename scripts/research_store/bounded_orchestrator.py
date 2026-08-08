"""Bounded acquisition/extraction stages for issue #216.

These stages preserve the existing orchestrator lifecycle and PostgreSQL
authority while separating search discovery from candidate provider extraction.
Each candidate provider call is independently bounded and audited before any
blob/corpus ingestion occurs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from budget_policy import DEFAULT_POLICY
from research_domain import load_model

from .bounded_acquisition import BoundedFirecrawlSearchAdapter
from .domain import IngestRequest, SearchAdapterResult, utcnow
from .orchestrator import (
    AcquisitionStage,
    ExtractionStage,
    _extraction_failure_class,
    _minimum_authoritative_source_target,
)
from .provider_preflight import (
    CandidatePreflightChecker,
    CandidatePreflightResult,
    extract_markdown,
    extract_response_metadata,
    redact_error_text,
    validate_candidate_url,
)
from .run_service import RunStateError, StaleRunRevisionError
from .stages import ContextKeys, StageResult

logger = logging.getLogger(__name__)

_TERMINAL_PREFLIGHT_CLASSES = frozenset(
    {
        "unsuitable_url",
        "empty_content",
        "anti_bot",
        "unsupported_content_type",
        "http_error",
        "timeout",
        "provider_error",
        "malformed",
        "transient",
    }
)


def _metadata_preflight(metadata: Mapping[str, Any]) -> CandidatePreflightResult | None:
    raw = metadata.get("preflight") or metadata.get("_preflight")
    if isinstance(raw, Mapping) and raw.get("classification"):
        return CandidatePreflightResult.from_metadata(raw)

    classification = metadata.get("preflight_classification")
    if not classification:
        return None
    classification = str(classification)
    terminal = metadata.get("preflight_terminal")
    if terminal is None:
        terminal = (
            classification in _TERMINAL_PREFLIGHT_CLASSES
            and classification != "suitable"
        )
    return CandidatePreflightResult(
        classification=classification,
        reason_code=str(metadata.get("preflight_reason_code") or classification),
        reason=redact_error_text(
            metadata.get("preflight_reason") or f"preflight {classification}"
        ),
        failure_stage=str(
            metadata.get("preflight_failure_stage") or "candidate_preflight"
        ),
        http_status=_safe_int(
            metadata.get("preflight_http_status")
            or (metadata.get("firecrawl") or {}).get("status_code")
        ),
        content_type=_safe_str(metadata.get("preflight_content_type")),
        elapsed_seconds=_safe_float(metadata.get("preflight_elapsed_seconds")),
        first_byte_seconds=_safe_float(metadata.get("preflight_first_byte_seconds")),
        provider_operation_seconds=_safe_float(
            metadata.get("preflight_provider_operation_seconds")
        ),
        cancelled=bool(metadata.get("preflight_cancelled", False)),
        retryable=bool(metadata.get("preflight_retryable", False)),
        terminal=bool(terminal),
    )


def _apply_preflight_metadata(
    metadata: dict[str, Any], outcome: CandidatePreflightResult
) -> None:
    metadata.update(
        {
            "preflight_classification": outcome.classification,
            "preflight_terminal": outcome.terminal,
            "preflight_cancelled": outcome.cancelled,
            "preflight_retryable": outcome.retryable,
            "preflight_reason_code": outcome.reason_code,
            "preflight_reason": redact_error_text(outcome.reason),
            "preflight_failure_stage": outcome.failure_stage,
            "preflight_http_status": outcome.http_status,
            "preflight_content_type": outcome.content_type,
            "preflight_elapsed_seconds": outcome.elapsed_seconds,
            "preflight_first_byte_seconds": outcome.first_byte_seconds,
            "preflight_provider_operation_seconds": (
                outcome.provider_operation_seconds
            ),
        }
    )


def _failure_class(classification: str) -> str:
    return {
        "unsuitable_url": "malformed",
        "empty_content": "empty_content",
        "anti_bot": "anti_bot",
        "unsupported_content_type": "unsupported_format",
        "http_error": "http_error",
        "timeout": "timeout",
        "transient": "network",
        "provider_error": "internal",
        "malformed": "malformed",
    }.get(classification, "internal")


def _audit_message(outcome: CandidatePreflightResult) -> str:
    elapsed = (
        f"{outcome.elapsed_seconds:.6f}"
        if outcome.elapsed_seconds is not None
        else "unknown"
    )
    return redact_error_text(
        "preflight failure; "
        f"class={outcome.classification}; reason_code={outcome.reason_code}; "
        f"stage={outcome.failure_stage}; elapsed_seconds={elapsed}; "
        f"cancelled={str(outcome.cancelled).lower()}; reason={outcome.reason}",
        max_chars=1000,
    )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str | None:
    return str(value) if value is not None else None


class BoundedAcquisitionStage(AcquisitionStage):
    """Discover candidates without embedding unbounded candidate scrapes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preflight_checker = CandidatePreflightChecker()

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

        authorized_proposals = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
        if authorized_proposals:
            existing_texts = {q.get("query", "") for q in queries}
            for proposal in authorized_proposals:
                for query in proposal.get("proposed_queries", []):
                    query_text = query.get("query", "")
                    if query_text and query_text not in existing_texts:
                        queries.append(query)
                        existing_texts.add(query_text)
        if not queries:
            return StageResult.failed(
                "acquisition", "Search plan has no queries to execute"
            )

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
        extraction_attempt_count = int(context.get("extraction_attempt_count", 0))
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
                backend = (
                    "firecrawl_scrape"
                    if query.get("facet") == "benchmark_source"
                    else "firecrawl"
                )
                result = self.acquisition_service.execute_search(
                    run_id,
                    query_text,
                    backend=backend,
                    idempotency_key=f"acquire:{run_id}:{query_text}",
                    limit=caps.results_per_branch,
                )
                executed_queries.add(query_text)
                response_ids.append(str(result.search_response_id))
                candidate_count += result.candidate_count
                response_transport = result.search_response.get(
                    "transport_metadata", {}
                )
                response_preflight = (
                    response_transport.get("preflight")
                    if isinstance(response_transport, Mapping)
                    else None
                )
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
                    if extraction_attempt_count >= attempt_target:
                        continue
                    scheduled_candidates.add(cid_str)

                    raw_item = cand.get("raw_item") or {}
                    provider_metadata = raw_item.get("metadata") or {}
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
                    elif (
                        isinstance(response_preflight, Mapping)
                        and backend == "firecrawl_scrape"
                    ):
                        preflight = CandidatePreflightResult.from_metadata(
                            response_preflight
                        )
                    else:
                        preflight = validate_candidate_url(str(url or ""))

                    markdown = raw_item.get("markdown")
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

        if raw_ingest_requests:
            try:
                self.run_service.transition(
                    run_id,
                    "extracting",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:acquisition_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="BoundedAcquisitionStage",
                    triggering_event="run.extracting",
                    reason=(
                        f"acquired {candidate_count} candidates for bounded extraction"
                    ),
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

        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision,
                idempotency_key=f"stage:acquisition_empty:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="BoundedAcquisitionStage",
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


class BoundedExtractionStage(ExtractionStage):
    """Run each candidate provider extraction under the issue #216 policy."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preflight_checker = CandidatePreflightChecker()
        self.scrape_adapter = BoundedFirecrawlSearchAdapter()

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
                "extraction", "candidates contain no authoritative acquisition records"
            )

        scrape_adapter = context.get("_candidate_scrape_adapter") or self.scrape_adapter
        attempt_by_manifest_ordinal: dict[int, dict[str, Any]] = {}
        active_requests: list[dict[str, Any]] = []
        terminal_count = 0
        cancelled_count = 0
        wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
        targets = context.get("candidate_coverage_items", {})

        for raw_ordinal, item in enumerate(raw_requests):
            metadata = dict(item.get("metadata", {}))
            candidate_raw = metadata.get("candidate_id")
            if not candidate_raw:
                return StageResult.failed(
                    "extraction", "extraction request is missing candidate provenance"
                )
            candidate_id = UUID(str(candidate_raw))
            requested_url = item.get("requested_url") or "unknown:"
            request = item.get("request")
            outcome = _metadata_preflight(metadata)
            provider_result: SearchAdapterResult | None = None
            attempt_started_at = utcnow()
            attempt_id = self.extraction_service.create_attempt(
                candidate_id=candidate_id,
                run_id=run_id,
                method="firecrawl_main_content",
                method_version="cli-1.19.27",
                requested_format="markdown",
                start_time=attempt_started_at,
            )

            if request is None and (outcome is None or not outcome.terminal):
                provider_result = scrape_adapter.scrape_url(str(requested_url))
                raw_preflight = provider_result.transport_metadata.get("preflight")
                if isinstance(raw_preflight, Mapping):
                    outcome = CandidatePreflightResult.from_metadata(raw_preflight)
                else:
                    outcome = self.preflight_checker.check(provider_result)
                _apply_preflight_metadata(metadata, outcome)
                item["metadata"] = metadata

                if not outcome.terminal:
                    provider_data = provider_result.raw_payload
                    markdown = extract_markdown(json.loads(provider_data))
                    if not isinstance(markdown, str) or not markdown.strip():
                        outcome = CandidatePreflightResult(
                            classification="empty_content",
                            reason_code="missing_usable_content",
                            reason="bounded provider result had no usable markdown",
                            failure_stage="content_suitability",
                            http_status=provider_result.http_status,
                            elapsed_seconds=_safe_float(
                                provider_result.transport_metadata.get(
                                    "elapsed_seconds"
                                )
                            ),
                            cancelled=True,
                            terminal=True,
                        )
                        _apply_preflight_metadata(metadata, outcome)
                    else:
                        provider_metadata = extract_response_metadata(
                            json.loads(provider_data)
                        )
                        request = IngestRequest(
                            requested_url=str(requested_url),
                            final_url=provider_metadata.get("url")
                            or provider_metadata.get("sourceURL")
                            or str(requested_url),
                            content=markdown.encode("utf-8"),
                            normalized_content=markdown.encode("utf-8"),
                            mime_type="text/markdown",
                            title=item.get("title") or provider_metadata.get("title"),
                            http_status=provider_result.http_status,
                            firecrawl_version="cli-1.19.27",
                            crawl_options={
                                "operation": "bounded candidate scrape",
                                "formats": ["markdown"],
                            },
                            metadata=metadata,
                        )
                        item["request"] = request

            if outcome is None and request is not None:
                synthetic = SearchAdapterResult(
                    raw_payload=(
                        b'{"markdown": '
                        + json.dumps(
                            request.content.decode("utf-8", errors="replace")
                        ).encode()
                        + b"}"
                    ),
                    http_status=request.http_status,
                )
                outcome = self.preflight_checker.check(synthetic)
                _apply_preflight_metadata(metadata, outcome)
                item["metadata"] = metadata

            if outcome is not None and outcome.terminal:
                failure_class = _failure_class(outcome.classification)
                self.extraction_service.complete_attempt(
                    attempt_id=attempt_id,
                    exit_status="cancelled" if outcome.cancelled else "failed",
                    failure_class=failure_class,
                    http_status=outcome.http_status,
                    backend_status=(
                        f"preflight:{outcome.failure_stage}:{outcome.reason_code}"
                    )[:500],
                    error_message=_audit_message(outcome),
                    end_time=(
                        provider_result.responded_at
                        if provider_result is not None
                        else None
                    ),
                )
                terminal_count += 1
                cancelled_count += int(outcome.cancelled)
                self._record_coverage_failure(
                    run_id,
                    candidate_id,
                    str(requested_url),
                    targets,
                    wave_count,
                )
                continue

            if request is None:
                fallback = CandidatePreflightResult(
                    classification="malformed",
                    reason_code="missing_ingest_request",
                    reason="candidate passed no authoritative content to ingestion",
                    failure_stage="candidate_preflight",
                    cancelled=False,
                    terminal=True,
                )
                self.extraction_service.complete_attempt(
                    attempt_id=attempt_id,
                    exit_status="failed",
                    failure_class="malformed",
                    backend_status=(
                        "preflight:candidate_preflight:missing_ingest_request"
                    ),
                    error_message=_audit_message(fallback),
                )
                terminal_count += 1
                self._record_coverage_failure(
                    run_id,
                    candidate_id,
                    str(requested_url),
                    targets,
                    wave_count,
                )
                continue

            raw_blob = self.extraction_service.store_raw_blob(request.content)
            normalized = request.normalized_content or request.content
            normalized_blob = self.extraction_service.store_normalized_blob(normalized)
            item["request"] = replace(request, extraction_attempt_id=attempt_id)
            manifest_ordinal = (
                metadata.get("firecrawl", {}).get("result_index")
                if isinstance(metadata.get("firecrawl"), Mapping)
                else None
            )
            manifest_ordinal = (
                int(manifest_ordinal)
                if isinstance(manifest_ordinal, int) and manifest_ordinal >= 0
                else raw_ordinal
            )
            attempt_by_manifest_ordinal[manifest_ordinal] = {
                "attempt_id": attempt_id,
                "candidate_id": candidate_id,
                "raw_blob": raw_blob,
                "normalized_blob": normalized_blob,
                "metadata": metadata,
            }
            active_requests.append(item)

        invocation_id = f"extract:{run_id}:w{wave_count}"
        run_status = self.run_service.status(run_id=run_id)
        if not run_status.external_id:
            return StageResult.failed(
                "extraction", "research run has no external ID for asset linkage"
            )

        if active_requests:
            try:
                manifest = self.corpus_service.ingest_batch(
                    invocation_id=invocation_id,
                    operation="orchestration_extract",
                    requests=active_requests,
                    research_run_external_id=run_status.external_id,
                    metadata={
                        "run_id": str(run_id),
                        "authority": "firecrawl-cli-1.19.27",
                        "bounded_preflight": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                safe_error = redact_error_text(exc)
                for attempt in attempt_by_manifest_ordinal.values():
                    self.extraction_service.complete_attempt(
                        attempt_id=attempt["attempt_id"],
                        exit_status="failed",
                        raw_blob=attempt["raw_blob"],
                        normalized_blob=attempt["normalized_blob"],
                        failure_class="internal",
                        backend_status="corpus_ingestion_failed",
                        error_message=safe_error,
                    )
                return StageResult.failed(
                    "extraction", f"authoritative corpus ingestion failed: {safe_error}"
                )
        else:
            manifest = {"assets": [], "failure_count": 0}

        completed_assets: list[dict[str, Any]] = []
        for asset in manifest.get("assets", []):
            ordinal = int(asset["ordinal"])
            attempt = attempt_by_manifest_ordinal.get(ordinal)
            if attempt is None:
                return StageResult.failed(
                    "extraction",
                    f"corpus manifest ordinal {ordinal} has no extraction attempt",
                )
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
                error_message=(
                    redact_error_text(asset.get("error"))
                    if asset.get("error")
                    else None
                ),
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
                selection_reason="bounded authoritative Firecrawl markdown persisted",
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

        extraction_success_count = len(completed_assets)
        context.setdefault("extracted_assets", []).extend(completed_assets)
        context[ContextKeys.EXTRACTION_SUCCESS_COUNT] = extraction_success_count
        context[ContextKeys.EXTRACTION_ATTEMPTS] = len(raw_requests)
        context["cancelled_extraction_count"] = cancelled_count
        context["preflight_terminal_count"] = terminal_count
        context["successful_extraction_count"] = (
            int(context.get("successful_extraction_count", 0))
            + extraction_success_count
        )

        if extraction_success_count > 0:
            try:
                self.run_service.transition(
                    run_id,
                    "indexing",
                    expected_revision=run_revision,
                    idempotency_key=f"stage:extraction_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="BoundedExtractionStage",
                    triggering_event="run.indexing",
                    reason=(
                        f"extraction succeeded for {extraction_success_count} sources"
                    ),
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
                    actor_identifier="BoundedExtractionStage",
                    triggering_event="run.coverage_review",
                    reason="no successful bounded extractions, reviewing coverage",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("extraction", str(exc))

        return StageResult.ok(
            "extraction",
            f"{extraction_success_count} successful extractions",
            details={
                ContextKeys.EXTRACTION_SUCCESS_COUNT: extraction_success_count,
                ContextKeys.EXTRACTION_ATTEMPTS: len(raw_requests),
                "cancelled_extraction_count": cancelled_count,
                "preflight_terminal_count": terminal_count,
                "extracted_assets": completed_assets,
            },
        )

    def _record_coverage_failure(
        self,
        run_id: UUID,
        candidate_id: UUID,
        source_url: str,
        targets: Mapping[str, list[str]],
        wave_count: int,
    ) -> None:
        for item_id in targets.get(str(candidate_id), []):
            self.coverage_service.apply_extraction_attempted(
                run_id,
                UUID(item_id),
                source_url=source_url,
                extraction_status="failed",
                idempotency_key=(
                    f"extract:{run_id}:w{wave_count}:{candidate_id}:{item_id}"
                ),
            )


__all__ = ["BoundedAcquisitionStage", "BoundedExtractionStage"]
