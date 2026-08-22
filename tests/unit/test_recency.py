"""Issue #300 AC5 regression coverage for the canonical recency parser."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.recency import (
    RecencyParseError,
    parse_recency_window,
    validate_recency_window,
)


@pytest.mark.parametrize(
    ("tbs", "expected_days"),
    [
        ("qdr:5d", 5),
        ("qdr:d", 1),
        ("qdr:1d", 1),
        ("qdr:2d", 2),
        ("qdr:30d", 30),
        ("qdr:w", 7),
        ("qdr:2w", 14),
        ("qdr:m", 31),
        ("qdr:2m", 62),
        ("qdr:y", 366),
        ("qdr:h", 1),
        ("qdr:12h", 1),
        ("qdr:24h", 1),
        ("qdr:25h", 2),
        ("qdr:48h", 2),
        ("qdr:72h", 3),
    ],
)
def test_parse_recency_window(tbs: str, expected_days: int) -> None:
    assert parse_recency_window(tbs) == expected_days


@pytest.mark.parametrize(
    "tbs",
    [
        "",
        "   ",
        "qdr:",
        "qdr:5",
        "qdr:5x",
        "qdr:0d",
        "qdr:01d",
        "qdr:-5d",
        "qdr:5 d",
        "qdr:d5",
        "recent:5d",
        "qdr:5D",
        "qdr:5h ",
    ],
)
def test_parse_recency_window_rejects_unsupported_syntax(tbs: str) -> None:
    with pytest.raises(RecencyParseError):
        parse_recency_window(tbs)


def test_validate_recency_window_accepts_none_empty_and_valid() -> None:
    validate_recency_window(None)
    validate_recency_window("")
    validate_recency_window("qdr:5d")


def test_validate_recency_window_rejects_unsupported_syntax() -> None:
    with pytest.raises(RecencyParseError):
        validate_recency_window("qdr:5x")


def test_invalid_syntax_is_value_error_but_none_passes() -> None:
    with pytest.raises(ValueError):
        parse_recency_window("qdr:5x")
    # None is a valid "no explicit window" value for the validation entry point.
    assert validate_recency_window(None) is None


def test_request_validation_fails_before_any_side_effects() -> None:
    from firecrawl_skill.research_store.fsearch_service import FSearchRequest

    with pytest.raises(RecencyParseError):
        FSearchRequest(
            query="test",
            research_run_id="fr_98d41ca8a16d4d25bffa50d1a8fb7425",
            tbs="qdr:5x",
        )

    request = FSearchRequest(
        query="test",
        research_run_id="fr_98d41ca8a16d4d25bffa50d1a8fb7425",
        tbs="qdr:5d",
    )
    assert request.tbs == "qdr:5d"
