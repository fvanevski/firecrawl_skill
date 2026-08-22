"""Canonical Firecrawl recency-window (``tbs``) parsing and normalization.

This module is the single parser for operator-supplied recency windows. The
grammar is ``qdr:<count><unit>`` where ``count`` is an optional positive
integer (defaults to 1, no leading zeros) and ``unit`` is one of ``h``,
``d``, ``w``, ``m`` or ``y``. Hour windows normalize to whole days because
freshness evaluation is day-granular, rounded up so the effective window is
never looser than requested. Unsupported syntax raises
:class:`RecencyParseError`; there is no silent fallback.
"""

from __future__ import annotations

import re

__all__ = [
    "RECENCY_UNITS",
    "RecencyParseError",
    "parse_recency_window",
]

# Day-equivalents of the supported whole-day recency units. ``h`` is
# sub-day and is normalized separately (see :func:`parse_recency_window`).
RECENCY_UNITS: dict[str, int] = {"d": 1, "w": 7, "m": 31, "y": 366}

_RECENTCY_PATTERN = re.compile(r"^qdr:(?P<count>[1-9]\d*)?(?P<unit>[hdwmy])$")


class RecencyParseError(ValueError):
    """Raised when a recency window does not follow the canonical grammar."""


def parse_recency_window(tbs: str) -> int:
    """Parse a canonical ``qdr:<count><unit>`` window into whole days."""
    match = _RECENTCY_PATTERN.fullmatch(tbs)
    if match is None:
        raise RecencyParseError(
            f"unsupported recency window {tbs!r}; expected "
            "qdr:<count><unit> with unit in {h, d, w, m, y} "
            "(count defaults to 1, no leading zeros)"
        )
    count = int(match.group("count") or "1")
    unit = match.group("unit")
    if unit == "h":
        return max(1, (count + 23) // 24)
    return count * RECENCY_UNITS[unit]


def validate_recency_window(tbs: str | None) -> None:
    """Fail closed on unsupported recency syntax before any provider call.

    ``None`` and the empty string carry no window and are accepted; any
    non-empty value must follow the canonical grammar exactly.
    """
    if not tbs:
        return
    parse_recency_window(tbs)
