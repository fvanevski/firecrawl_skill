from __future__ import annotations

import pytest

from firecrawl_skill.research_store.planner_yield_diagnostic import (
    compare_planner_yield,
)


def test_offline_harness_captures_zero_scoped_vs_nonzero_unscoped_yield() -> None:
    payload = compare_planner_yield(
        scoped_query="example event site:example.test",
        unscoped_query="example event",
        scoped_candidate_count=0,
        unscoped_candidate_count=7,
        planner_provenance={"planner": "fixture", "revision": 2},
    )
    assert payload["comparison"]["observation"] == "scoped_zero_unscoped_nonzero"
    assert payload["scoped"]["query_shape"]["site_scoped"] is True
    assert payload["unscoped"]["query_shape"]["site_scoped"] is False
    assert payload["planner_provenance"]["revision"] == 2
    assert payload["production_planner_change_authorized"] is False


def test_harness_rejects_invalid_scope_pair() -> None:
    with pytest.raises(ValueError, match="scoped_query"):
        compare_planner_yield(
            scoped_query="example event",
            unscoped_query="example event",
            scoped_candidate_count=0,
            unscoped_candidate_count=0,
            planner_provenance={},
        )


def test_harness_contains_no_provider_specific_domain_policy() -> None:
    import inspect

    from firecrawl_skill.research_store import planner_yield_diagnostic

    source = inspect.getsource(planner_yield_diagnostic).casefold()
    assert "reuters" not in source
    assert "associated press" not in source
    assert "apnews" not in source
