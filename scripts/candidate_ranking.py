"""Candidate URL classification, ranking scores, and corpus budget enforcement.

This module classifies candidate URLs into structural types (article, live_blog,
official_release, topic_hub, home_page, reference_page, search_page, unknown),
computes deterministic ranking scores that penalise generic high-volume pages,
and enforces configurable corpus budgets with explicit override justification.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any

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


# ---------------------------------------------------------------------------
# URL-type heuristics
# ---------------------------------------------------------------------------

_URL_TYPE_PATTERNS: list[tuple[UrlType, tuple[str, ...]]] = [
    (
        UrlType.LIVE_BLOG,
        (
            "/live-blog/",
            "/live-updates/",
            "/live-blog",
            "/live-updates",
            "/liveblog/",
            "liveblog",
            "live coverage",
            "live blog",
        ),
    ),
    (
        UrlType.OFFICIAL_RELEASE,
        (
            "/press-release/",
            "/press_releases/",
            "/releases/",
            "/newsroom/",
            "prnewswire",
            "business wire",
            "release.pr",
        ),
    ),
    (
        UrlType.TOPIC_HUB,
        (
            "/topics/",
            "/topic/",
            "/category/",
            "/categories/",
            "/tag/",
            "/tags/",
            "/section/",
            "/sections/",
            "/hub/",
            "/hub",
            "topic hub",
            "topic center",
        ),
    ),
    (
        UrlType.HOME_PAGE,
        (
            "/",
            "/index.html",
            "/default.aspx",
        ),
    ),
    (
        UrlType.REFERENCE_PAGE,
        (
            "/faq",
            "/faq/",
            "/help",
            "/help/",
            "/support",
            "/support/",
            "/terms",
            "/privacy",
            "/cookies",
            "/about",
            "/about/",
            "/contact",
            "/sitemap",
            "/disclaimer",
            "/legal",
        ),
    ),
    (
        UrlType.SEARCH_PAGE,
        (
            "/search",
            "/search/",
            "/query",
            "/results",
            "/results/",
            "?q=",
            "?search=",
            "?query=",
        ),
    ),
]

# Domains that are commonly topic hubs rather than articles.
_HUB_DOMAINS = frozenset(
    {
        "reddit.com",
        "medium.com",
        "hubspot.com",
        "forbes.com",
        "linkedin.com",
    }
)

# Domains that publish official releases.
_RELEASE_DOMAINS = frozenset(
    {
        "prnewswire.com",
        "businesswire.com",
        "globenewswire.com",
    }
)

_DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\w{3,9}[-/]\d{2,4}"
    r"|\w{3,9}[-/]\d{1,2}[-/]\d{2,4})\b"
)

_STALE_THRESHOLD_DAYS = 365


def classify_url(url: str, title: str = "", snippet: str = "") -> UrlType:
    """Classify a candidate URL into a structural type.

    The classifier is deterministic and operates only on the URL path, domain,
    title, and snippet. It never invokes external services.
    """
    url_lower = url.lower()
    text_scan = f"{title} {snippet}".lower()

    # Live blog — most specific match first.
    for pattern in (
        "/live-blog/",
        "/live-updates/",
        "/live-blog",
        "/live-updates",
        "/liveblog/",
    ):
        if pattern in url_lower:
            return UrlType.LIVE_BLOG
    for keyword in ("liveblog", "live coverage", "live blog"):
        if keyword in text_scan:
            return UrlType.LIVE_BLOG

    # Official release.
    for pattern in ("/press-release/", "/press_releases/", "/releases/", "/newsroom/"):
        if pattern in url_lower:
            return UrlType.OFFICIAL_RELEASE
    for domain in _RELEASE_DOMAINS:
        if domain in url_lower:
            return UrlType.OFFICIAL_RELEASE
    for keyword in ("prnewswire", "business wire", "release.pr"):
        if keyword in text_scan:
            return UrlType.OFFICIAL_RELEASE

    # Search page.
    for pattern in ("/search", "/search/", "/query", "/results", "/results/"):
        if pattern in url_lower:
            return UrlType.SEARCH_PAGE
    for param in ("?q=", "?search=", "?query="):
        if param in url_lower:
            return UrlType.SEARCH_PAGE

    # Topic hub — path-based or domain-based.
    for pattern in (
        "/topics/",
        "/topic/",
        "/category/",
        "/categories/",
        "/tag/",
        "/tags/",
        "/section/",
        "/sections/",
        "/hub/",
        "/hub",
    ):
        if pattern in url_lower:
            return UrlType.TOPIC_HUB
    for keyword in ("topic hub", "topic center"):
        if keyword in text_scan:
            return UrlType.TOPIC_HUB
    registered_domain = _registered_domain(url_lower)
    if registered_domain in _HUB_DOMAINS:
        return UrlType.TOPIC_HUB

    # Home page.
    path_match = re.match(r"https?://[^/]+(/.*)?", url_lower)
    path = path_match.group(1) if path_match else "/"
    if path in ("/", "/index.html", "/index.htm", "/default.aspx"):
        return UrlType.HOME_PAGE

    # Reference page.
    for pattern in (
        "/faq",
        "/faq/",
        "/help",
        "/help/",
        "/support",
        "/support/",
        "/terms",
        "/privacy",
        "/cookies",
        "/about",
        "/about/",
        "/contact",
        "/sitemap",
        "/disclaimer",
        "/legal",
    ):
        if path.endswith(pattern) or path == pattern:
            return UrlType.REFERENCE_PAGE

    # Default to article when nothing else matches.
    return UrlType.ARTICLE


def _registered_domain(url: str) -> str:
    """Extract registered domain from a URL for heuristic matching."""
    m = re.match(r"https?://([^/]+)", url)
    if not m:
        return ""
    host = m.group(1).lower()
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def assess_freshness(
    published_at: datetime | None,
    retrieved_at: datetime,
) -> tuple[FreshnessStatus, str]:
    """Return freshness status and rationale for a candidate.

    A candidate whose published date is more than ``_STALE_THRESHOLD_DAYS`` ago
    is treated as stale. Missing dates are flagged as unassessed rather than
    stale so downstream consumers can decide how to handle them.
    """
    if published_at is None:
        return FreshnessStatus.NOT_APPLICABLE, "no published date available"
    age_days = (retrieved_at - published_at).days
    if age_days < 0:
        return FreshnessStatus.SATISFIED, f"published in the future ({age_days} days)"
    if age_days <= 7:
        return FreshnessStatus.SATISFIED, f"published {age_days} days ago"
    if age_days <= _STALE_THRESHOLD_DAYS:
        return FreshnessStatus.UNSATISFIED, f"published {age_days} days ago"
    return (
        FreshnessStatus.UNSATISFIED,
        f"published {age_days} days ago — exceeds stale threshold",
    )


# ---------------------------------------------------------------------------
# Ranking score
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankingScore:
    """Deterministic composite ranking score for a single candidate.

    Attributes:
        base_score: Provider-supplied rank translated to a 0–1 scale.
        url_type_penalty: Penalty for generic/high-volume URL structures.
        freshness_penalty: Penalty derived from publication recency.
        duplication_penalty: Penalty for content already seen in the corpus.
        size_penalty: Penalty for extreme expected content size.
        total: Sum of base_score plus all penalties (range 0–1).
        rationale: Human-readable explanation of the score components.
    """

    base_score: float
    url_type_penalty: float
    freshness_penalty: float
    duplication_penalty: float
    size_penalty: float
    total: float
    rationale: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.base_score <= 1.0:
            raise ValueError("base_score must be in [0, 1]")
        if not 0.0 <= self.url_type_penalty <= 1.0:
            raise ValueError("url_type_penalty must be in [0, 1]")
        if not 0.0 <= self.freshness_penalty <= 1.0:
            raise ValueError("freshness_penalty must be in [0, 1]")
        if not 0.0 <= self.duplication_penalty <= 1.0:
            raise ValueError("duplication_penalty must be in [0, 1]")
        if not 0.0 <= self.size_penalty <= 1.0:
            raise ValueError("size_penalty must be in [0, 1]")
        computed = self.base_score - (
            self.url_type_penalty
            + self.freshness_penalty
            + self.duplication_penalty
            + self.size_penalty
        )
        clamped = max(0.0, min(1.0, computed))
        if abs(self.total - clamped) > 1e-9:
            object.__setattr__(self, "total", clamped)


# ---------------------------------------------------------------------------
# Ranking policy configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankingPolicy:
    """Configurable penalties applied during candidate ranking.

    All penalty fields are in [0, 1]. Higher values produce stronger penalties.
    """

    generic_page_penalty: float = 0.3
    home_page_penalty: float = 0.5
    reference_page_penalty: float = 0.2
    stale_date_penalty: float = 0.4
    duplication_penalty: float = 0.6
    extreme_size_penalty: float = 0.3
    extreme_size_large_threshold: int = 500_000
    extreme_size_small_threshold: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "generic_page_penalty",
            "home_page_penalty",
            "reference_page_penalty",
            "stale_date_penalty",
            "duplication_penalty",
            "extreme_size_penalty",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, float)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be a float in [0, 1]")
        if self.extreme_size_large_threshold < 0:
            raise ValueError("extreme_size_large_threshold must be >= 0")
        if self.extreme_size_small_threshold < 0:
            raise ValueError("extreme_size_small_threshold must be >= 0")


DEFAULT_RANKING_POLICY = RankingPolicy()


def compute_ranking_score(
    base_score: float,
    url_type: UrlType,
    freshness_status: FreshnessStatus,
    is_duplicate: bool,
    expected_char_count: int | None,
    *,
    policy: RankingPolicy | None = None,
) -> RankingScore:
    """Compute a deterministic ranking score for a candidate.

    Generic high-volume pages (topic hubs, home pages, reference pages) receive
    penalties that prevent them from dominating narrowly scoped research. Stale
    dates, duplicates, and extreme sizes are also penalised.
    """
    if policy is None:
        policy = DEFAULT_RANKING_POLICY

    # URL-type penalty.
    url_penalty = 0.0
    if url_type == UrlType.TOPIC_HUB:
        url_penalty = policy.generic_page_penalty
    elif url_type == UrlType.HOME_PAGE:
        url_penalty = policy.home_page_penalty
    elif url_type == UrlType.REFERENCE_PAGE:
        url_penalty = policy.reference_page_penalty

    # Freshness penalty.
    freshness_penalty = 0.0
    if freshness_status == FreshnessStatus.UNSATISFIED:
        freshness_penalty = policy.stale_date_penalty

    # Duplication penalty.
    dup_penalty = policy.duplication_penalty if is_duplicate else 0.0

    # Size penalty.
    size_penalty = 0.0
    if expected_char_count is not None:
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
        total=base_score
        - (url_penalty + freshness_penalty + dup_penalty + size_penalty),
        rationale="; ".join(rationale_parts),
    )


# ---------------------------------------------------------------------------
# Corpus budget enforcement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateBudget:
    """Hard and soft limits governing corpus composition.

    Hard limits are enforced strictly; soft limits require explicit recorded
    justification to override.
    """

    max_candidates: int = 40
    max_bytes: int = 5_000_000
    max_chunks: int = 2000
    max_per_asset_contribution_chunks: int = 500
    max_generic_page_share: float = 0.25
    max_exploratory_extraction_attempts: int = 10

    def __post_init__(self) -> None:
        hard_fields = (
            "max_candidates",
            "max_bytes",
            "max_chunks",
            "max_exploratory_extraction_attempts",
        )
        for name in hard_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        soft_fields = (
            "max_per_asset_contribution_chunks",
            "max_generic_page_share",
        )
        for name in soft_fields:
            value = getattr(self, name)
            if name == "max_per_asset_contribution_chunks":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
            else:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, float)
                    or not 0.0 <= value <= 1.0
                ):
                    raise ValueError(f"{name} must be a float in [0, 1]")


@dataclass(frozen=True)
class BudgetViolation:
    """Records a single budget limit violation."""

    limit_name: str
    limit_value: float | int
    observed_value: float | int
    is_hard: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetCheckResult:
    """Result of enforcing corpus budget constraints.

    Attributes:
        violations: List of violated limits.
        soft_violations: Soft-limit violations that can be overridden.
        hard_violations: Hard-limit violations that cannot be overridden.
        requires_override: True when soft violations exist.
    """

    violations: tuple[BudgetViolation, ...]
    soft_violations: tuple[BudgetViolation, ...]
    hard_violations: tuple[BudgetViolation, ...]
    requires_override: bool

    @property
    def accepted(self) -> bool:
        return len(self.hard_violations) == 0

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
    """Explicit recorded justification to override a soft budget limit.

    The justification must identify which limit is being overridden, why, and
    by whom (or by what automated rule). Overrides are append-only and immutable
    once recorded.
    """

    limit_name: str
    reason: str
    author: str
    created_at: datetime
    run_id: str

    def __post_init__(self) -> None:
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
    """Enforce corpus budget constraints and return violations.

    Hard limits (max_candidates, max_bytes, max_chunks,
    max_exploratory_extraction_attempts) are strict. Soft limits
    (max_per_asset_contribution_chunks, max_generic_page_share) can be
    overridden with explicit justification.
    """
    if budget is None:
        budget = CandidateBudget()

    violations: list[BudgetViolation] = []
    soft_violations: list[BudgetViolation] = []
    hard_violations: list[BudgetViolation] = []

    candidate_count = len(candidates)
    if candidate_count > budget.max_candidates:
        v = BudgetViolation(
            limit_name="max_candidates",
            limit_value=budget.max_candidates,
            observed_value=candidate_count,
            is_hard=True,
            message=(
                f"Candidate count {candidate_count} exceeds hard limit "
                f"{budget.max_candidates}"
            ),
        )
        hard_violations.append(v)
        violations.append(v)

    if total_bytes > budget.max_bytes:
        v = BudgetViolation(
            limit_name="max_bytes",
            limit_value=budget.max_bytes,
            observed_value=total_bytes,
            is_hard=True,
            message=(
                f"Total bytes {total_bytes} exceeds hard limit {budget.max_bytes}"
            ),
        )
        hard_violations.append(v)
        violations.append(v)

    if total_chunks > budget.max_chunks:
        v = BudgetViolation(
            limit_name="max_chunks",
            limit_value=budget.max_chunks,
            observed_value=total_chunks,
            is_hard=True,
            message=(
                f"Total chunks {total_chunks} exceeds hard limit {budget.max_chunks}"
            ),
        )
        hard_violations.append(v)
        violations.append(v)

    if extraction_attempts > budget.max_exploratory_extraction_attempts:
        v = BudgetViolation(
            limit_name="max_exploratory_extraction_attempts",
            limit_value=budget.max_exploratory_extraction_attempts,
            observed_value=extraction_attempts,
            is_hard=True,
            message=(
                f"Extraction attempts {extraction_attempts} exceeds hard limit "
                f"{budget.max_exploratory_extraction_attempts}"
            ),
        )
        hard_violations.append(v)
        violations.append(v)

    # Soft limit: generic page share.
    if candidate_count > 0:
        generic_share = generic_page_count / candidate_count
        if generic_share > budget.max_generic_page_share:
            v = BudgetViolation(
                limit_name="max_generic_page_share",
                limit_value=budget.max_generic_page_share,
                observed_value=generic_share,
                is_hard=False,
                message=(
                    f"Generic page share {generic_share:.3f} exceeds soft limit "
                    f"{budget.max_generic_page_share}"
                ),
            )
            soft_violations.append(v)
            violations.append(v)

    # Soft limit: per-asset contribution.
    for asset_id, chunk_count in per_asset_chunk_counts.items():
        if chunk_count > budget.max_per_asset_contribution_chunks:
            v = BudgetViolation(
                limit_name="max_per_asset_contribution_chunks",
                limit_value=budget.max_per_asset_contribution_chunks,
                observed_value=chunk_count,
                is_hard=False,
                message=(
                    f"Asset {asset_id} contributed {chunk_count} chunks, "
                    f"exceeding soft limit {budget.max_per_asset_contribution_chunks}"
                ),
            )
            soft_violations.append(v)
            violations.append(v)

    return BudgetCheckResult(
        violations=tuple(violations),
        soft_violations=tuple(soft_violations),
        hard_violations=tuple(hard_violations),
        requires_override=len(soft_violations) > 0,
    )


def validate_override_justification(
    justification: OverrideJustification,
    allowed_limits: Sequence[str] | None = None,
) -> None:
    """Validate an override justification against allowed limits.

    Raises ValueError if the justification references an unknown limit or if
    required fields are missing.
    """
    if allowed_limits is not None and justification.limit_name not in allowed_limits:
        raise ValueError(
            f"override limit '{justification.limit_name}' not in allowed limits"
        )
    if allowed_limits is not None and justification.limit_name not in allowed_limits:
        raise ValueError(
            f"override limit '{justification.limit_name}' not in allowed limits"
        )
    if allowed_limits is not None and justification.limit_name not in allowed_limits:
        raise ValueError(
            f"override limit '{justification.limit_name}' not in allowed limits"
        )
    if allowed_limits is not None and justification.limit_name not in allowed_limits:
        raise ValueError(
            f"override limit '{justification.limit_name}' not in allowed limits"
        )
    if allowed_limits is not None and justification.limit_name not in allowed_limits:
        raise ValueError(
            f"override limit '{justification.limit_name}' not in allowed limits"
        )


# Type alias for sequence-like objects used by check_corpus_budget.
from collections.abc import Mapping, Sequence

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
    "validate_override_justification",
]
