"""Candidate URL classification, ranking scores, and corpus budget policy.

The module is deliberately pure: it classifies persisted candidate metadata,
computes deterministic ranking scores, and evaluates candidate/corpus budgets.
PostgreSQL persistence and override authorization live in
``research_store.candidate_policy_service``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from research_domain.models import FreshnessStatus


class UrlType(str, Enum):
    """Structural classification of a candidate URL."""

    ARTICLE = "article"
    LIVE_BLOG = "live_blog"
    OFFICIAL_RELEASE = "official_release"
    TOPIC_HUB = "topic_hub"
    HOME_PAGE = "home_page"
    REFERENCE_PAGE = "reference_page"
    SEARCH_PAGE = "search_page"
    UNKNOWN = "unknown"


_HUB_DOMAINS = frozenset(
    {"reddit.com", "medium.com", "hubspot.com", "forbes.com", "linkedin.com"}
)
_RELEASE_DOMAINS = frozenset(
    {"prnewswire.com", "businesswire.com", "globenewswire.com"}
)
_REFERENCE_DOMAINS = frozenset({"wikipedia.org"})
_GENERIC_URL_TYPES = frozenset(
    {
        UrlType.TOPIC_HUB,
        UrlType.HOME_PAGE,
        UrlType.REFERENCE_PAGE,
        UrlType.SEARCH_PAGE,
        UrlType.UNKNOWN,
    }
)
_STALE_THRESHOLD_DAYS = 365


def _registered_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host.lower()


def classify_url(url: str, title: str = "", snippet: str = "") -> UrlType:
    """Classify one HTTP(S) URL without inferring missing structure.

    Malformed, scheme-less, and non-HTTP(S) values are ``unknown``. Wikipedia
    article pages are treated as reference pages for the narrow-objective
    ranking policy; this is a ranking penalty, not a universal invalidation.
    """

    value = (url or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return UrlType.UNKNOWN
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return UrlType.UNKNOWN

    host = parsed.hostname.lower()
    registered_domain = _registered_domain(host)
    path = parsed.path.lower() or "/"
    query = parsed.query.lower()
    text_scan = f"{title} {snippet}".lower()

    if any(marker in path for marker in ("/live-blog", "/live-updates", "/liveblog")):
        return UrlType.LIVE_BLOG
    if any(marker in text_scan for marker in ("liveblog", "live coverage", "live blog")):
        return UrlType.LIVE_BLOG

    if any(
        marker in path
        for marker in ("/press-release/", "/press_releases/", "/releases/", "/newsroom/")
    ):
        return UrlType.OFFICIAL_RELEASE
    if registered_domain in _RELEASE_DOMAINS:
        return UrlType.OFFICIAL_RELEASE
    if any(marker in text_scan for marker in ("prnewswire", "business wire", "release.pr")):
        return UrlType.OFFICIAL_RELEASE

    if any(marker in path for marker in ("/search", "/query", "/results")):
        return UrlType.SEARCH_PAGE
    if any(
        query == name or query.startswith(f"{name}=") or f"&{name}=" in f"&{query}"
        for name in ("q", "search", "query")
    ):
        return UrlType.SEARCH_PAGE

    if any(
        marker in path
        for marker in (
            "/topics/",
            "/topic/",
            "/category/",
            "/categories/",
            "/tag/",
            "/tags/",
            "/section/",
            "/sections/",
            "/hub/",
        )
    ) or path.endswith("/hub"):
        return UrlType.TOPIC_HUB
    if any(marker in text_scan for marker in ("topic hub", "topic center")):
        return UrlType.TOPIC_HUB
    if registered_domain in _HUB_DOMAINS:
        return UrlType.TOPIC_HUB

    if path in {"/", "/index.html", "/index.htm", "/default.aspx"}:
        return UrlType.HOME_PAGE

    if registered_domain in _REFERENCE_DOMAINS:
        return UrlType.REFERENCE_PAGE
    if any(
        path == marker or path.endswith(marker)
        for marker in (
            "/faq",
            "/help",
            "/support",
            "/terms",
            "/privacy",
            "/cookies",
            "/about",
            "/contact",
            "/sitemap",
            "/disclaimer",
            "/legal",
        )
    ):
        return UrlType.REFERENCE_PAGE

    return UrlType.ARTICLE


def is_generic_url_type(url_type: UrlType) -> bool:
    return url_type in _GENERIC_URL_TYPES


def assess_freshness(
    published_at: datetime | None,
    retrieved_at: datetime,
    *,
    stale_after_days: int = _STALE_THRESHOLD_DAYS,
) -> tuple[FreshnessStatus, str]:
    """Return freshness status consistent with the declared stale threshold."""

    if stale_after_days < 0:
        raise ValueError("stale_after_days must be non-negative")
    if published_at is None:
        return FreshnessStatus.NOT_APPLICABLE, "no published date available"
    try:
        age_days = (retrieved_at - published_at).days
    except TypeError as exc:
        raise ValueError("published_at and retrieved_at must use compatible timezones") from exc
    if age_days < 0:
        return FreshnessStatus.SATISFIED, f"published in the future ({age_days} days)"
    if age_days <= stale_after_days:
        return FreshnessStatus.SATISFIED, f"published {age_days} days ago"
    return (
        FreshnessStatus.UNSATISFIED,
        f"published {age_days} days ago — exceeds stale threshold {stale_after_days}",
    )


@dataclass(frozen=True)
class RankingScore:
    base_score: float
    url_type_penalty: float
    freshness_penalty: float
    duplication_penalty: float
    size_penalty: float
    total: float
    rationale: str

    def __post_init__(self) -> None:
        for name in (
            "base_score",
            "url_type_penalty",
            "freshness_penalty",
            "duplication_penalty",
            "size_penalty",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        computed = self.base_score - (
            self.url_type_penalty
            + self.freshness_penalty
            + self.duplication_penalty
            + self.size_penalty
        )
        clamped = max(0.0, min(1.0, computed))
        if abs(self.total - clamped) > 1e-9:
            object.__setattr__(self, "total", clamped)


@dataclass(frozen=True)
class RankingPolicy:
    generic_page_penalty: float = 0.3
    home_page_penalty: float = 0.5
    reference_page_penalty: float = 0.2
    search_page_penalty: float = 0.4
    unknown_page_penalty: float = 0.25
    stale_date_penalty: float = 0.4
    duplication_penalty: float = 0.6
    extreme_size_penalty: float = 0.3
    extreme_size_large_threshold: int = 500_000
    extreme_size_small_threshold: int = 1_000
    stale_after_days: int = _STALE_THRESHOLD_DAYS

    def __post_init__(self) -> None:
        for name in (
            "generic_page_penalty",
            "home_page_penalty",
            "reference_page_penalty",
            "search_page_penalty",
            "unknown_page_penalty",
            "stale_date_penalty",
            "duplication_penalty",
            "extreme_size_penalty",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, float) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a float in [0, 1]")
        for name in (
            "extreme_size_large_threshold",
            "extreme_size_small_threshold",
            "stale_after_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.extreme_size_small_threshold > self.extreme_size_large_threshold:
            raise ValueError("small size threshold cannot exceed large size threshold")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RankingPolicy:
        env = os.environ if environ is None else environ
        defaults = cls()

        def f(name: str, current: float) -> float:
            return float(env.get(name, current))

        def i(name: str, current: int) -> int:
            return int(env.get(name, current))

        return cls(
            generic_page_penalty=f("FIRECRAWL_RANK_GENERIC_PAGE_PENALTY", defaults.generic_page_penalty),
            home_page_penalty=f("FIRECRAWL_RANK_HOME_PAGE_PENALTY", defaults.home_page_penalty),
            reference_page_penalty=f("FIRECRAWL_RANK_REFERENCE_PAGE_PENALTY", defaults.reference_page_penalty),
            search_page_penalty=f("FIRECRAWL_RANK_SEARCH_PAGE_PENALTY", defaults.search_page_penalty),
            unknown_page_penalty=f("FIRECRAWL_RANK_UNKNOWN_PAGE_PENALTY", defaults.unknown_page_penalty),
            stale_date_penalty=f("FIRECRAWL_RANK_STALE_DATE_PENALTY", defaults.stale_date_penalty),
            duplication_penalty=f("FIRECRAWL_RANK_DUPLICATION_PENALTY", defaults.duplication_penalty),
            extreme_size_penalty=f("FIRECRAWL_RANK_EXTREME_SIZE_PENALTY", defaults.extreme_size_penalty),
            extreme_size_large_threshold=i("FIRECRAWL_RANK_LARGE_CHAR_THRESHOLD", defaults.extreme_size_large_threshold),
            extreme_size_small_threshold=i("FIRECRAWL_RANK_SMALL_CHAR_THRESHOLD", defaults.extreme_size_small_threshold),
            stale_after_days=i("FIRECRAWL_RANK_STALE_AFTER_DAYS", defaults.stale_after_days),
        )


DEFAULT_RANKING_POLICY = RankingPolicy()


def rank_to_base_score(rank: float | str | None, candidate_count: int) -> float:
    """Translate a provider ordinal rank to a deterministic [0, 1] score."""

    if candidate_count <= 0:
        return 0.5
    try:
        ordinal = int(rank) if rank is not None else candidate_count
    except (TypeError, ValueError):
        ordinal = candidate_count
    ordinal = min(max(ordinal, 1), candidate_count)
    if candidate_count == 1:
        return 1.0
    return 1.0 - ((ordinal - 1) / candidate_count)


def compute_ranking_score(
    base_score: float,
    url_type: UrlType,
    freshness_status: FreshnessStatus,
    is_duplicate: bool,
    expected_char_count: int | None,
    *,
    policy: RankingPolicy | None = None,
) -> RankingScore:
    policy = policy or DEFAULT_RANKING_POLICY

    url_penalty = {
        UrlType.TOPIC_HUB: policy.generic_page_penalty,
        UrlType.HOME_PAGE: policy.home_page_penalty,
        UrlType.REFERENCE_PAGE: policy.reference_page_penalty,
        UrlType.SEARCH_PAGE: policy.search_page_penalty,
        UrlType.UNKNOWN: policy.unknown_page_penalty,
    }.get(url_type, 0.0)
    freshness_penalty = (
        policy.stale_date_penalty
        if freshness_status == FreshnessStatus.UNSATISFIED
        else 0.0
    )
    dup_penalty = policy.duplication_penalty if is_duplicate else 0.0
    size_penalty = 0.0
    if expected_char_count is not None:
        if expected_char_count < 0:
            raise ValueError("expected_char_count must be non-negative")
        if expected_char_count >= policy.extreme_size_large_threshold:
            size_penalty = policy.extreme_size_penalty
        elif expected_char_count <= policy.extreme_size_small_threshold:
            size_penalty = policy.extreme_size_penalty * 0.5

    rationale_parts = [
        f"base={base_score:.3f}",
        f"url_type={url_type.value} penalty={url_penalty:.2f}",
        f"freshness={freshness_status.value} penalty={freshness_penalty:.2f}",
        f"duplication={'yes' if is_duplicate else 'no'} penalty={dup_penalty:.2f}",
        f"size={expected_char_count} penalty={size_penalty:.2f}",
    ]
    return RankingScore(
        base_score=base_score,
        url_type_penalty=url_penalty,
        freshness_penalty=freshness_penalty,
        duplication_penalty=dup_penalty,
        size_penalty=size_penalty,
        total=base_score - (url_penalty + freshness_penalty + dup_penalty + size_penalty),
        rationale="; ".join(rationale_parts),
    )


@dataclass(frozen=True)
class CandidateBudget:
    max_candidates: int = 40
    max_bytes: int = 5_000_000
    max_chunks: int = 2000
    max_per_asset_contribution_chunks: int = 500
    max_generic_page_share: float = 0.25
    max_exploratory_extraction_attempts: int = 10

    def __post_init__(self) -> None:
        for name in (
            "max_candidates",
            "max_bytes",
            "max_chunks",
            "max_per_asset_contribution_chunks",
            "max_exploratory_extraction_attempts",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            isinstance(self.max_generic_page_share, bool)
            or not isinstance(self.max_generic_page_share, float)
            or not 0.0 <= self.max_generic_page_share <= 1.0
        ):
            raise ValueError("max_generic_page_share must be a float in [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CandidateBudget:
        env = os.environ if environ is None else environ
        defaults = cls()
        return cls(
            max_candidates=int(env.get("FIRECRAWL_BUDGET_MAX_CANDIDATES", defaults.max_candidates)),
            max_bytes=int(env.get("FIRECRAWL_BUDGET_MAX_BYTES", defaults.max_bytes)),
            max_chunks=int(env.get("FIRECRAWL_BUDGET_MAX_CHUNKS", defaults.max_chunks)),
            max_per_asset_contribution_chunks=int(
                env.get(
                    "FIRECRAWL_BUDGET_MAX_PER_ASSET_CHUNKS",
                    defaults.max_per_asset_contribution_chunks,
                )
            ),
            max_generic_page_share=float(
                env.get("FIRECRAWL_BUDGET_MAX_GENERIC_PAGE_SHARE", defaults.max_generic_page_share)
            ),
            max_exploratory_extraction_attempts=int(
                env.get(
                    "FIRECRAWL_BUDGET_MAX_EXTRACTION_ATTEMPTS",
                    defaults.max_exploratory_extraction_attempts,
                )
            ),
        )


@dataclass(frozen=True)
class BudgetViolation:
    limit_name: str
    limit_value: float | int
    observed_value: float | int
    is_hard: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetCheckResult:
    violations: tuple[BudgetViolation, ...]
    soft_violations: tuple[BudgetViolation, ...]
    hard_violations: tuple[BudgetViolation, ...]
    requires_override: bool

    @property
    def accepted(self) -> bool:
        """True only when no policy limit is violated without an override."""
        return not self.violations

    def accepted_with_overrides(self, overridden_limits: Sequence[str]) -> bool:
        if self.hard_violations:
            return False
        allowed = set(overridden_limits)
        return all(v.limit_name in allowed for v in self.soft_violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "violations": [v.to_dict() for v in self.violations],
            "soft_violations": [v.to_dict() for v in self.soft_violations],
            "hard_violations": [v.to_dict() for v in self.hard_violations],
            "requires_override": self.requires_override,
        }


@dataclass(frozen=True)
class OverrideJustification:
    limit_name: str
    reason: str
    author: str
    created_at: datetime
    run_id: str
    budget_check_id: str | None = None

    def __post_init__(self) -> None:
        if not self.limit_name.strip():
            raise ValueError("limit_name must be non-empty")
        if not self.reason.strip():
            raise ValueError("reason must be non-empty")
        if not self.author.strip():
            raise ValueError("author must be non-empty")
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


def check_corpus_budget(
    candidates: Sequence[Any],
    total_bytes: int,
    total_chunks: int,
    generic_page_count: int,
    extraction_attempts: int,
    per_asset_chunk_counts: Mapping[str, int],
    *,
    budget: CandidateBudget | None = None,
) -> BudgetCheckResult:
    budget = budget or CandidateBudget()
    for name, value in (
        ("total_bytes", total_bytes),
        ("total_chunks", total_chunks),
        ("generic_page_count", generic_page_count),
        ("extraction_attempts", extraction_attempts),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    violations: list[BudgetViolation] = []
    soft: list[BudgetViolation] = []
    hard: list[BudgetViolation] = []

    def add(name: str, limit: float, observed: float, *, is_hard: bool) -> None:
        violation = BudgetViolation(
            limit_name=name,
            limit_value=limit,
            observed_value=observed,
            is_hard=is_hard,
            message=f"{name} observed={observed} exceeds limit={limit}",
        )
        violations.append(violation)
        (hard if is_hard else soft).append(violation)

    candidate_count = len(candidates)
    if candidate_count > budget.max_candidates:
        add("max_candidates", budget.max_candidates, candidate_count, is_hard=True)
    if total_bytes > budget.max_bytes:
        add("max_bytes", budget.max_bytes, total_bytes, is_hard=True)
    if total_chunks > budget.max_chunks:
        add("max_chunks", budget.max_chunks, total_chunks, is_hard=True)
    if extraction_attempts > budget.max_exploratory_extraction_attempts:
        add(
            "max_exploratory_extraction_attempts",
            budget.max_exploratory_extraction_attempts,
            extraction_attempts,
            is_hard=True,
        )

    if generic_page_count > candidate_count:
        raise ValueError("generic_page_count cannot exceed candidate_count")
    if candidate_count:
        generic_share = generic_page_count / candidate_count
        if generic_share > budget.max_generic_page_share:
            add(
                "max_generic_page_share",
                budget.max_generic_page_share,
                generic_share,
                is_hard=False,
            )

    for asset_id, chunk_count in sorted(per_asset_chunk_counts.items()):
        if isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or chunk_count < 0:
            raise ValueError(f"chunk count for {asset_id} must be a non-negative integer")
        if chunk_count > budget.max_per_asset_contribution_chunks:
            add(
                "max_per_asset_contribution_chunks",
                budget.max_per_asset_contribution_chunks,
                chunk_count,
                is_hard=False,
            )

    return BudgetCheckResult(
        violations=tuple(violations),
        soft_violations=tuple(soft),
        hard_violations=tuple(hard),
        requires_override=bool(soft),
    )


def validate_override_justification(
    justification: OverrideJustification,
    allowed_limits: Sequence[str] | None = None,
) -> None:
    if allowed_limits is not None and justification.limit_name not in set(allowed_limits):
        raise ValueError(f"override limit '{justification.limit_name}' not in allowed limits")


__all__ = [
    "DEFAULT_RANKING_POLICY",
    "BudgetCheckResult",
    "BudgetViolation",
    "CandidateBudget",
    "OverrideJustification",
    "RankingPolicy",
    "RankingScore",
    "UrlType",
    "assess_freshness",
    "check_corpus_budget",
    "classify_url",
    "compute_ranking_score",
    "is_generic_url_type",
    "rank_to_base_score",
    "validate_override_justification",
]
