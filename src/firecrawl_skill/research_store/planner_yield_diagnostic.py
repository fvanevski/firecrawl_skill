"""Offline diagnostic contract for comparing planned query yield.

This module is intentionally observational. It does not alter planner queries,
provider parameters, domain restrictions, or acquisition behavior.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SITE_SCOPE = re.compile(r"(?:^|\s)site:(?P<domain>[^\s]+)", re.IGNORECASE)


def query_shape(query: str) -> dict[str, Any]:
    normalized = " ".join(query.split())
    domains = tuple(match.group("domain") for match in _SITE_SCOPE.finditer(normalized))
    return {
        "normalized_query": normalized,
        "site_scoped": bool(domains),
        "site_domains": list(domains),
        "term_count": len(normalized.split()),
    }


def compare_planner_yield(
    *,
    scoped_query: str,
    unscoped_query: str,
    scoped_candidate_count: int,
    unscoped_candidate_count: int,
    planner_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if scoped_candidate_count < 0 or unscoped_candidate_count < 0:
        raise ValueError("candidate counts must be non-negative")
    scoped_shape = query_shape(scoped_query)
    unscoped_shape = query_shape(unscoped_query)
    if not scoped_shape["site_scoped"]:
        raise ValueError("scoped_query must contain an explicit site: restriction")
    if unscoped_shape["site_scoped"]:
        raise ValueError("unscoped_query must not contain a site: restriction")

    if scoped_candidate_count == 0 and unscoped_candidate_count > 0:
        observation = "scoped_zero_unscoped_nonzero"
    elif scoped_candidate_count == unscoped_candidate_count:
        observation = "equal_yield"
    elif scoped_candidate_count < unscoped_candidate_count:
        observation = "scoped_lower_yield"
    else:
        observation = "scoped_higher_yield"

    return {
        "schema_version": "planner-yield-diagnostic-v1",
        "planner_provenance": dict(planner_provenance),
        "scoped": {
            "query": scoped_query,
            "query_shape": scoped_shape,
            "candidate_count": scoped_candidate_count,
        },
        "unscoped": {
            "query": unscoped_query,
            "query_shape": unscoped_shape,
            "candidate_count": unscoped_candidate_count,
        },
        "comparison": {
            "observation": observation,
            "candidate_count_delta": unscoped_candidate_count - scoped_candidate_count,
        },
        "production_planner_change_authorized": False,
    }


__all__ = ["compare_planner_yield", "query_shape"]
