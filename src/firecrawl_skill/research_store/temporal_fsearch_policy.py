"""Exact-recency ranking semantics for the production fsearch service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import replace
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain.models import FreshnessStatus

from . import fsearch_policy_service as _policy_module
from .acquisition.candidate_ranking import compute_ranking_score
from .fsearch_policy_service import (
    PolicyFSearchService,
    _published_at,
    _RankedCandidate,
)
from .fsearch_service import FSearchRequest, FSearchResult
from .recency import RecencyWindow, normalize_recency_window


class TemporalPolicyFSearchService(PolicyFSearchService):
    """Treat an undated candidate as unsatisfied for an explicit recency query.

    Unbounded searches retain historical ranking behavior.  The explicit-qdr
    path is fail closed: a provider hit with no authoritative publication date
    cannot receive the neutral ``not_applicable`` ranking treatment merely
    because the backend returned it for a coarse recency filter.
    """

    _recency_window: ContextVar[RecencyWindow | None] = ContextVar(
        "fsearch_exact_recency_window", default=None
    )

    def execute(self, request: FSearchRequest) -> FSearchResult:
        token = self._recency_window.set(normalize_recency_window(request.tbs))
        try:
            return super().execute(request)
        finally:
            self._recency_window.reset(token)

    def _rank_candidates(
        self,
        run_id: UUID,
        candidates: Sequence[Mapping[str, Any]],
        *,
        stale_after_days: int,
    ) -> list[_RankedCandidate]:
        ranked = super()._rank_candidates(
            run_id,
            candidates,
            stale_after_days=stale_after_days,
        )
        window = self._recency_window.get()
        if window is None:
            return ranked
        adjusted: list[_RankedCandidate] = []
        # Reuse the base policy module's ranking clock. Besides keeping exact and
        # coarse ranking in one time domain, this preserves the established test
        # and operator seam that can freeze evaluation time deterministically.
        evaluated_at = _policy_module.utcnow()
        for item in ranked:
            persisted = self.run_service.get_candidate(item.candidate_id, run_id=run_id)
            published_at = _published_at(persisted, item.candidate)
            if published_at is None:
                status = FreshnessStatus.UNSATISFIED
                rationale = (
                    "explicit recency requested but no authoritative publication "
                    "date is available"
                )
            else:
                try:
                    age_seconds = (evaluated_at - published_at).total_seconds()
                except TypeError as exc:
                    raise ValueError(
                        "published_at and ranking time must use compatible timezones"
                    ) from exc
                if age_seconds < 0:
                    status = FreshnessStatus.SATISFIED
                    rationale = "published in the future relative to ranking time"
                elif age_seconds <= window.exact_seconds:
                    status = FreshnessStatus.SATISFIED
                    rationale = (
                        f"publication age {int(age_seconds)}s is within exact "
                        f"{window.requested_tbs} window ({window.exact_seconds}s)"
                    )
                else:
                    status = FreshnessStatus.UNSATISFIED
                    rationale = (
                        f"publication age {int(age_seconds)}s exceeds exact "
                        f"{window.requested_tbs} window ({window.exact_seconds}s)"
                    )
            score = compute_ranking_score(
                item.score.base_score,
                item.url_type,
                status,
                item.is_duplicate,
                item.expected_char_count,
                policy=self.ranking_policy,
            )
            adjusted.append(
                replace(
                    item,
                    freshness_status=status,
                    freshness_rationale=rationale,
                    score=score,
                )
            )
        adjusted.sort(
            key=lambda item: (
                -item.score.total,
                item.source_rank,
                str(item.candidate_id),
            )
        )
        return adjusted


__all__ = ["TemporalPolicyFSearchService"]
