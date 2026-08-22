"""Focused provider-response suitability regressions for issue #297."""

from __future__ import annotations

import json

from firecrawl_skill.research_store.acquisition.models import ScrapeTransportResult
from firecrawl_skill.research_store.acquisition.provider_response import (
    assess_scrape_transport_result,
)
from firecrawl_skill.research_store.provider_preflight import CandidatePreflightChecker


def _assess(
    payload: object,
    *,
    request_format: str = "markdown",
):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return assess_scrape_transport_result(
        ScrapeTransportResult(raw_payload=raw),
        CandidatePreflightChecker(),
        effective_format=request_format,
    )


def test_zero_exit_observed_406_empty_markdown_is_http_failure() -> None:
    assessment = _assess(
        {
            "markdown": "",
            "metadata": {"statusCode": 406, "error": "Not Acceptable"},
        }
    )

    assert assessment.suitable is False
    assert assessment.classification == "http_error"
    assert assessment.failure_class == "http_error"
    assert assessment.transport.http_status == 406


def test_explicit_provider_error_without_http_failure_is_rejected() -> None:
    assessment = _assess(
        {
            "markdown": "# Provider returned a body",
            "metadata": {"error": "upstream rejected request"},
        }
    )

    assert assessment.suitable is False
    assert assessment.classification == "provider_error"
    assert assessment.reason_code == "provider_transport_error"
    assert assessment.failure_class == "malformed"


def test_provider_content_type_is_normalized_into_shared_preflight() -> None:
    assessment = _assess(
        {
            "markdown": "non-empty payload",
            "metadata": {"statusCode": 200, "contentType": "application/zip"},
        }
    )

    assert assessment.suitable is False
    assert assessment.classification == "unsupported_content_type"
    assert assessment.failure_class == "unsupported_format"


def test_structured_provider_envelope_without_content_is_empty() -> None:
    assessment = _assess({"data": {"web": [{"metadata": {"statusCode": 200}}]}})

    assert assessment.suitable is False
    assert assessment.classification == "empty_content"
    assert assessment.failure_class == "empty_content"


def test_requested_structured_json_remains_usable_content_without_adapter_metadata() -> None:
    assessment = _assess(
        {"result": {"answer": 42}, "error": "domain field, not provider failure"},
        request_format="json",
    )

    assert assessment.suitable is True
    assert assessment.classification == "suitable"
    assert assessment.failure_class == "none"


def test_json_is_not_reinterpreted_as_structured_content_for_markdown_request() -> None:
    assessment = _assess({"answer": 42})

    assert assessment.suitable is False
    assert assessment.classification == "empty_content"


def test_raw_markdown_still_passes_shared_suitability_policy() -> None:
    assessment = _assess(b"# Useful direct scrape\n\nAuthoritative content.")

    assert assessment.suitable is True
    assert assessment.classification == "suitable"
