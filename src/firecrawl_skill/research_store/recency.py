"""Canonical exact recency parsing and provider-filter normalization.

Operator/request semantics and provider transport semantics are deliberately
separate.  A request such as ``qdr:5d`` means exactly five days to local
ranking, evidence, and completion policy.  Firecrawl is given only a documented
coarse filter that is a *superset* of that exact window; local policy remains
authoritative.  Unsupported syntax never falls back to a default window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "RECENCY_UNITS",
    "RecencyParseError",
    "RecencyWindow",
    "normalize_recency_window",
    "parse_recency_seconds",
    "parse_recency_window",
    "validate_recency_window",
]

RECENCY_UNITS: dict[str, int] = {"d": 1, "w": 7, "m": 31, "y": 366}
_RECENCY_UNIT_SECONDS: dict[str, int] = {
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "m": 31 * 24 * 60 * 60,
    "y": 366 * 24 * 60 * 60,
}
_RECENCY_PATTERN = re.compile(r"^qdr:(?P<count>[1-9]\d*)?(?P<unit>[hdwmy])$")


class RecencyParseError(ValueError):
    """Raised when an explicit recency window is unsupported."""


@dataclass(frozen=True)
class RecencyWindow:
    """Exact local recency semantics plus the provider discovery filter."""

    requested_tbs: str
    exact_seconds: int
    exact_days: int
    provider_tbs: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_tbs": self.requested_tbs,
            "exact_seconds": self.exact_seconds,
            # Compatibility/display field: whole days rounded up. Ranking uses
            # exact_seconds so sub-day requests are never weakened.
            "exact_days": self.exact_days,
            "provider_tbs": self.provider_tbs,
            "authority": "local_exact_window",
        }


def _parts(tbs: str) -> tuple[int, str]:
    match = _RECENCY_PATTERN.fullmatch(tbs)
    if match is None:
        raise RecencyParseError(
            f"unsupported recency window {tbs!r}; expected "
            "qdr:<count><unit> with unit in {h, d, w, m, y} "
            "(count defaults to 1, no leading zeros)"
        )
    return int(match.group("count") or "1"), match.group("unit")


def parse_recency_seconds(tbs: str) -> int:
    """Return the exact requested recency duration in seconds."""
    count, unit = _parts(tbs)
    return count * _RECENCY_UNIT_SECONDS[unit]


def parse_recency_window(tbs: str) -> int:
    """Return whole-day compatibility width, rounded up for sub-day requests."""
    seconds = parse_recency_seconds(tbs)
    day = _RECENCY_UNIT_SECONDS["d"]
    return max(1, (seconds + day - 1) // day)


def _provider_superset(tbs: str, exact_seconds: int) -> str | None:
    """Return the smallest documented Firecrawl filter covering the request.

    Firecrawl documents qdr:h/d/w/m/y, not arbitrary counted forms.  Values
    larger than the documented yearly window deliberately receive no provider
    date filter; exact local filtering still applies and a narrower provider
    filter is never fabricated.
    """
    count, unit = _parts(tbs)
    if unit == "h" and count == 1:
        return "qdr:h"
    day = _RECENCY_UNIT_SECONDS["d"]
    if exact_seconds <= day:
        return "qdr:d"
    if exact_seconds <= 7 * day:
        return "qdr:w"
    if exact_seconds <= 31 * day:
        return "qdr:m"
    if exact_seconds <= 366 * day:
        return "qdr:y"
    return None


def normalize_recency_window(tbs: str | None) -> RecencyWindow | None:
    """Normalize one explicit request without weakening its exact semantics."""
    if not tbs:
        return None
    exact_seconds = parse_recency_seconds(tbs)
    exact_days = parse_recency_window(tbs)
    return RecencyWindow(
        requested_tbs=tbs,
        exact_seconds=exact_seconds,
        exact_days=exact_days,
        provider_tbs=_provider_superset(tbs, exact_seconds),
    )


def validate_recency_window(tbs: str | None) -> None:
    """Fail closed before provider access when explicit syntax is unsupported."""
    if tbs:
        parse_recency_window(tbs)
