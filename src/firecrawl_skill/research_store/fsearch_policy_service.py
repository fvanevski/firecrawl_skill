"""Policy-complete PostgreSQL-authoritative ``fsearch`` implementation.

This module is the production entry point for issue #215.  It preserves the
existing fsearch transport/CLI contract while adding persisted candidate
rankings and fail-closed corpus-budget gates around selected extraction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from firecrawl_skill.research_store.acquisition.authority import (
    AcquisitionPreflightError,
)
from firecrawl_skill.research_store.acquisition.candidate_ranking import (
    CandidateBudget,
    RankingPolicy,
    UrlType,
    assess_freshness,
    classify_url,
    compute_ranking_score,
    rank_to_base_score,
)
from firecrawl_skill.research_store.acquisition.direct_scrape_application import (
    DirectScrapeError,
    DirectScrapePersistenceError,
)
from firecrawl_skill.research_store.acquisition.models import DirectScrapeBatchResult
from firecrawl_skill.research_store.acquisition.service import AcquisitionResult
from firecrawl_skill.research_store.composition import build_direct_scrape_service

from .candidate_policy_service import (
    BudgetDecision,
    CandidatePolicyError,
    CandidatePolicyService,
    decision_error_message,
)
from .config import StoreConfig
from .domain import utcnow
from .fsearch_service import (
    FSearchError,
    FSearchRequest,
    FSearchResult,
    FSearchService,
    MetadataOnlyFirecrawlSearchAdapter,
    _candidate_uuid,
    _default_search_key,
    _exception_stage,
    _extraction_key,
    _outcome_from_item,
    _result_failure_stage,
    new_invocation_id,
)


class PolicyFSearchError(FSearchError):
    """FSearch failure carrying stable policy/audit detail."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        result: FSearchResult | None = None,
        reason_code: str | None = None,
        budget_decision: BudgetDecision | None = None,
    ) -> None:
        super().__init__(stage, message, result=result)
        self.reason_code = reason_code
        self.budget_decision = budget_decision

    def to_dict(self) -> dict[str, Any]:
        value = super().to_dict()
        if self.reason_code is not None:
            value["reason_code"] = self.reason_code
        if self.budget_decision is not None:
            value["budget"] = self.budget_decision.summary()
        return value


@dataclass(frozen=True)
class _RankedCandidate:
    candidate: Mapping[str, Any]
    candidate_id: UUID
    source_rank: int
    url: str
    url_type: UrlType
    freshness_status: Any
    freshness_rationale: str
    stale_after_days: int
    is_duplicate: bool
    expected_char_count: int | None
    score: Any


class PolicyFSearchService(FSearchService):
    """FSearch with persisted ranking provenance and two-phase budget gates."""

    def __init__(
        self,
        *args: Any,
        policy_service: CandidatePolicyService,
        ranking_policy: RankingPolicy | None = None,
        candidate_budget: CandidateBudget | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.policy_service = policy_service
        self.ranking_policy = ranking_policy or RankingPolicy.from_env()
        self.candidate_budget = candidate_budget or CandidateBudget.from_env()

    def execute(self, request: FSearchRequest) -> FSearchResult:
        run_status = self._resolve_run(request.research_run_id)
        try:
            context = self.preflight(run_id=run_status.id, config=self.config)
        except AcquisitionPreflightError as exc:
            raise FSearchError("preflight", str(exc)) from exc
        except Exception as exc:
            raise FSearchError("preflight", str(exc)) from exc

        external_invocation_id = request.external_invocation_id or new_invocation_id()
        search_key = request.idempotency_key or _default_search_key(
            run_status.id, request, external_invocation_id
        )
        invocation = self.invocation_service.begin(
            run_status.id,
            external_invocation_id,
            "fsearch",
            {
                "schema_version": "authoritative-fsearch-v1",
                "query": request.query,
                "limit": request.limit,
                "scrape_limit": request.scrape_limit,
                "sources": request.sources,
                "tbs": request.tbs,
                "profile": request.profile,
                "search_idempotency_key": search_key,
            },
            idempotency_key=f"fsearch-invocation:{external_invocation_id}",
        )
        invocation_id = UUID(str(invocation.id))
        base_result = FSearchResult(
            status="running",
            run_id=run_status.id,
            research_run_id=request.research_run_id,
            invocation_id=invocation_id,
            external_invocation_id=external_invocation_id,
            search_response_id=None,
            candidate_ids=(),
        )

        try:
            acquisition = self.acquisition_factory().execute_search(
                run_status.id,
                request.query,
                idempotency_key=search_key,
                limit=request.limit,
                sources=request.sources,
                tbs=request.tbs,
                authority_context=context,
                replay_existing=True,
                metadata={
                    "invocation_id": str(invocation_id),
                    "classification_profile": request.profile,
                    "selected_scrape_limit": request.scrape_limit,
                    "implicit_scrape": False,
                    "ranking_policy": self.ranking_policy.__dict__,
                    "candidate_budget": self.candidate_budget.to_dict(),
                },
            )
            result = self._after_search(request, base_result, acquisition, search_key)
        except PolicyFSearchError as exc:
            self._fail_policy_invocation(run_status.id, invocation_id, exc)
            raise
        except FSearchError as exc:
            self._fail_invocation(
                run_status.id,
                invocation_id,
                exc.stage,
                str(exc),
                exc.result or base_result,
            )
            raise
        except Exception as exc:
            stage = _exception_stage(exc)
            failure = FSearchError(stage, str(exc), result=base_result)
            self._fail_invocation(
                run_status.id,
                invocation_id,
                stage,
                str(exc),
                base_result,
            )
            raise failure from exc

        terminal = "succeeded" if result.status in {"complete", "empty"} else "failed"
        self.invocation_service.complete(
            run_status.id,
            invocation_id,
            terminal,
            output=result.to_dict(),
            error=result.error,
        )
        if terminal != "succeeded":
            stage = _result_failure_stage(result)
            raise FSearchError(
                stage,
                result.error or "one or more selected extractions failed",
                result=result,
            )
        return result

    def _after_search(
        self,
        request: FSearchRequest,
        base_result: FSearchResult,
        acquisition: AcquisitionResult,
        search_key: str,
    ) -> FSearchResult:
        candidate_ids = tuple(_candidate_uuid(item) for item in acquisition.candidates)
        searched = FSearchResult(
            **{
                **asdict(base_result),
                "search_response_id": UUID(str(acquisition.search_response_id)),
                "candidate_ids": candidate_ids,
                "search_replayed": acquisition.replayed,
            }
        )
        if not acquisition.postgres_committed:
            raise FSearchError(
                "ingestion",
                "search response was not committed to PostgreSQL",
                result=searched,
            )
        if acquisition.status == "provider_error":
            error = (
                acquisition.search_response.get("error_message")
                or "Firecrawl search transport failed"
            )
            raise FSearchError("search_transport", str(error), result=searched)
        if acquisition.status == "parse_error":
            error = (
                acquisition.search_response.get("error_message")
                or "Firecrawl search response could not be parsed"
            )
            raise FSearchError("candidate_parsing", str(error), result=searched)
        if acquisition.status not in {"succeeded", "empty"}:
            error = (
                acquisition.search_response.get("error_message")
                or f"unexpected search response status: {acquisition.status}"
            )
            raise FSearchError("candidate_parsing", str(error), result=searched)
        if acquisition.status == "empty":
            return FSearchResult(**{**asdict(searched), "status": "empty"})

        ranked = self._rank_candidates(
            searched.run_id,
            acquisition.candidates,
            stale_after_days=_stale_after_days(request.tbs, self.ranking_policy),
        )
        selected = ranked[: request.scrape_limit]
        ranking_rows = self._ranking_rows(ranked, request.scrape_limit)
        try:
            self.policy_service.record_rankings(
                searched.run_id,
                UUID(str(acquisition.search_response_id)),
                searched.invocation_id,
                ranking_rows,
            )
            pre = self.policy_service.evaluate_pre_extraction(
                searched.run_id,
                searched.invocation_id,
                ranking_rows,
                [item.candidate_id for item in selected],
                self.candidate_budget,
            )
        except CandidatePolicyError as exc:
            raise FSearchError(
                "ingestion",
                f"candidate policy persistence failed: {exc}",
                result=searched,
            ) from exc
        self._require_budget(pre, searched)

        if request.scrape_limit == 0 or not selected:
            return FSearchResult(**{**asdict(searched), "status": "complete"})

        requests = tuple(
            self._scrape_request(item.candidate, request.profile) for item in selected
        )
        try:
            extraction: DirectScrapeBatchResult = self.direct_scrape_factory().execute(
                searched.run_id,
                requests,
                idempotency_key=_extraction_key(search_key, request, requests),
                parent_invocation_id=searched.invocation_id,
            )
        except DirectScrapePersistenceError as exc:
            raise FSearchError(exc.stage, str(exc), result=searched) from exc
        except DirectScrapeError as exc:
            raise FSearchError("extraction", str(exc), result=searched) from exc

        outcomes = tuple(_outcome_from_item(item) for item in extraction.items)
        status = "complete" if extraction.status == "complete" else extraction.status
        error = None
        if status != "complete":
            failed = [item for item in outcomes if item.status != "succeeded"]
            error = (
                failed[0].error
                if failed and failed[0].error
                else "one or more selected extractions failed"
            )
        extracted = FSearchResult(
            **{
                **asdict(searched),
                "status": status,
                "extraction_invocation_id": UUID(str(extraction.invocation_id)),
                "extraction_status": extraction.status,
                "extraction_replayed": extraction.replayed,
                "extraction_outcomes": outcomes,
                "error": error,
            }
        )
        if status == "complete":
            try:
                post = self.policy_service.evaluate_post_extraction(
                    searched.run_id,
                    searched.invocation_id,
                    self.candidate_budget,
                )
            except CandidatePolicyError as exc:
                raise FSearchError(
                    "ingestion",
                    f"candidate post-extraction policy persistence failed: {exc}",
                    result=extracted,
                ) from exc
            self._require_budget(post, extracted)
        return extracted

    def _rank_candidates(
        self,
        run_id: UUID,
        candidates: Sequence[Mapping[str, Any]],
        *,
        stale_after_days: int,
    ) -> list[_RankedCandidate]:
        ranked: list[_RankedCandidate] = []
        count = len(candidates)
        for candidate in candidates:
            candidate_id = _candidate_uuid(candidate)
            try:
                persisted = self.run_service.get_candidate(candidate_id, run_id=run_id)
            except Exception as exc:
                raise FSearchError(
                    "ingestion",
                    f"persisted candidate lookup failed for {candidate_id}: {exc}",
                ) from exc
            url = str(
                persisted.get("canonical_url") or candidate.get("canonical_url") or ""
            )
            title = str(persisted.get("title") or candidate.get("title") or "")
            snippet = str(persisted.get("snippet") or candidate.get("snippet") or "")
            url_type = classify_url(url, title, snippet)
            published_at = _published_at(persisted, candidate)
            freshness_status, freshness_rationale = assess_freshness(
                published_at,
                utcnow(),
                stale_after_days=stale_after_days,
            )
            is_duplicate = persisted.get("duplicate_group_id") is not None
            expected_char_count = _expected_char_count(persisted, candidate)
            source_rank = _source_rank(candidate.get("rank"), count)
            base_score = rank_to_base_score(source_rank, count)
            score = compute_ranking_score(
                base_score,
                url_type,
                freshness_status,
                is_duplicate,
                expected_char_count,
                policy=self.ranking_policy,
            )
            ranked.append(
                _RankedCandidate(
                    candidate=candidate,
                    candidate_id=candidate_id,
                    source_rank=source_rank,
                    url=url,
                    url_type=url_type,
                    freshness_status=freshness_status,
                    freshness_rationale=freshness_rationale,
                    stale_after_days=stale_after_days,
                    is_duplicate=is_duplicate,
                    expected_char_count=expected_char_count,
                    score=score,
                )
            )
        ranked.sort(
            key=lambda item: (
                -item.score.total,
                item.source_rank,
                str(item.candidate_id),
            )
        )
        return ranked

    @staticmethod
    def _ranking_rows(
        ranked: Sequence[_RankedCandidate], scrape_limit: int
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(ranked):
            selected = index < scrape_limit
            selected_ordinal = index if selected else None
            decision = "selected" if selected else "rejected"
            decision_reason = (
                f"selected ordinal={index} within scrape_limit={scrape_limit}"
                if selected
                else f"rejected outside scrape_limit={scrape_limit}"
            )
            rows.append(
                {
                    "candidate_id": item.candidate_id,
                    "source_rank": item.source_rank,
                    "url": item.url,
                    "url_type": item.url_type.value,
                    "base_score": item.score.base_score,
                    "url_type_penalty": item.score.url_type_penalty,
                    "freshness_status": item.freshness_status.value,
                    "freshness_penalty": item.score.freshness_penalty,
                    "is_duplicate": item.is_duplicate,
                    "duplication_penalty": item.score.duplication_penalty,
                    "expected_char_count": item.expected_char_count,
                    "size_penalty": item.score.size_penalty,
                    "total_score": item.score.total,
                    "rationale": (
                        f"{item.score.rationale}; freshness_window_days="
                        f"{item.stale_after_days}; {item.freshness_rationale}; "
                        f"{decision_reason}"
                    ),
                    "decision": decision,
                    "selected_ordinal": selected_ordinal,
                    "decision_reason": decision_reason,
                }
            )
        return rows

    @staticmethod
    def _require_budget(decision: BudgetDecision, result: FSearchResult) -> None:
        if decision.accepted:
            return
        raise PolicyFSearchError(
            "ingestion",
            decision_error_message(decision),
            result=result,
            reason_code=decision.reason_code,
            budget_decision=decision,
        )

    def _fail_policy_invocation(
        self, run_id: UUID, invocation_id: UUID, error: PolicyFSearchError
    ) -> None:
        output = {
            **(error.result.to_dict() if error.result is not None else {}),
            "failure_stage": error.stage,
            "reason_code": error.reason_code,
        }
        if error.budget_decision is not None:
            output["budget"] = error.budget_decision.summary()
        try:
            self.invocation_service.complete(
                run_id,
                invocation_id,
                "failed",
                output=output,
                error=str(error)[:500],
            )
        except Exception:  # noqa: BLE001, S110
            pass


def _stale_after_days(tbs: str | None, policy: RankingPolicy) -> int:
    """Resolve an explicit Firecrawl recency window without inventing one."""

    if not tbs:
        return policy.stale_after_days
    value = tbs.strip().lower()
    fixed = {
        "qdr:h": 1,
        "qdr:d": 1,
        "qdr:w": 7,
        "qdr:m": 31,
        "qdr:y": 366,
    }
    if value in fixed:
        return fixed[value]
    if value.startswith("qdr:d") and value[5:].isdigit():
        return max(int(value[5:]), 0)
    return policy.stale_after_days


def _source_rank(value: Any, candidate_count: int) -> int:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return max(candidate_count, 1)
    return min(max(rank, 1), max(candidate_count, 1))


def _published_at(
    persisted: Mapping[str, Any], occurrence: Mapping[str, Any]
) -> datetime | None:
    value = persisted.get("published_at")
    if isinstance(value, datetime):
        return _timezone_aware(value)
    date_signals = persisted.get("date_signals") or {}
    if isinstance(date_signals, Mapping):
        value = date_signals.get("published_date")
    if value is None:
        raw_item = occurrence.get("raw_item") or {}
        if isinstance(raw_item, Mapping):
            value = (
                raw_item.get("published_at")
                or raw_item.get("publishedDate")
                or raw_item.get("date")
            )
    return _parse_datetime(value)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return _timezone_aware(datetime.fromisoformat(normalized))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _timezone_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _expected_char_count(
    persisted: Mapping[str, Any], occurrence: Mapping[str, Any]
) -> int | None:
    containers: list[Mapping[str, Any]] = []
    backend = persisted.get("backend_metadata")
    if isinstance(backend, Mapping):
        containers.append(backend)
    raw_item = occurrence.get("raw_item")
    if isinstance(raw_item, Mapping):
        containers.append(raw_item)
    for data in containers:
        for key in (
            "expected_char_count",
            "content_length",
            "contentLength",
            "char_count",
        ):
            raw = data.get(key)
            try:
                value = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                continue
            if value is not None and value >= 0:
                return value
        for key in ("markdown", "content", "text"):
            raw = data.get(key)
            if isinstance(raw, str):
                return len(raw)
    return None


def build_policy_fsearch_service(
    config: StoreConfig | None = None,
    *,
    search_adapter_factory=MetadataOnlyFirecrawlSearchAdapter,
) -> PolicyFSearchService:
    from firecrawl_skill.research_store.composition import (
        build_acquisition_service,
        build_invocation_service,
        build_run_service,
    )

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    run_service = build_run_service(resolved)
    return PolicyFSearchService(
        resolved,
        run_service,
        build_invocation_service(resolved),
        acquisition_factory=lambda: build_acquisition_service(
            resolved, search_adapter=search_adapter_factory()
        ),
        direct_scrape_factory=lambda: build_direct_scrape_service(resolved),
        policy_service=CandidatePolicyService(run_service.uow_factory),
    )


def main(argv: Sequence[str] | None = None) -> int:
    from .fsearch_service import main as legacy_main

    return legacy_main(argv, service_factory=build_policy_fsearch_service)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PolicyFSearchError",
    "PolicyFSearchService",
    "build_policy_fsearch_service",
    "main",
]
