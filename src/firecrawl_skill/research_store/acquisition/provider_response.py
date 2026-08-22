"""Provider response suitability assessment for authoritative direct scrapes.

A completed transport (exit 0, non-empty body) is not necessarily a usable
acquisition. The provider can return a non-2xx HTTP status, an explicit provider
error, an anti-bot interstitial, or an empty/unsupported body while the Firecrawl
CLI still exits cleanly. This module adapts a ``ScrapeTransportResult`` to the
shared candidate-preflight policy and reports a single deterministic assessment
that the direct-scrape application service uses to decide authoritative success
versus failure persistence.

The assessment only classifies provider-side rejections. It never mutates the
raw provider payload or invents provenance; it normalizes provider metadata into
the existing preflight contract and preserves the original transport for
failure provenance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..domain import SearchAdapterResult
from ..provider_preflight import (
    CandidatePreflightChecker,
    extract_markdown,
    extract_response_metadata,
    redact_diagnostic_value,
    redact_error_text,
)
from .models import ScrapeTransportResult

_MAX_PROVIDER_RESPONSE_CHARS = 1000
_MAX_DIAGNOSTIC_CHARS = 500
_STRUCTURED_CONTENT_FORMATS = frozenset({"json", "links", "images"})

# Map shared preflight classifications onto the direct-scrape failure classes
# accepted by ``DirectScrapeService._persist_failure``.
_CLASSIFICATION_TO_FAILURE_CLASS: dict[str, str] = {
    "http_error": "http_error",
    "transient": "network",
    "timeout": "timeout",
    "empty_content": "empty_content",
    "anti_bot": "anti_bot",
    "unsupported_content_type": "unsupported_format",
    "provider_error": "malformed",
    "malformed": "malformed",
    "unsuitable_url": "malformed",
}


@dataclass(frozen=True)
class ProviderResponseAssessment:
    """Determine whether a completed transport produced a usable acquisition."""

    suitable: bool
    transport: ScrapeTransportResult
    classification: str
    reason_code: str
    reason: str
    failure_class: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suitable": self.suitable,
            "classification": self.classification,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "failure_class": self.failure_class,
            "http_status": self.transport.http_status,
        }


def _parse_payload(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _is_json_payload(payload: bytes) -> bool:
    if not payload:
        return False
    try:
        json.loads(payload.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def _provider_http_status(
    transport: ScrapeTransportResult,
    provider_response: Mapping[str, Any],
) -> int | None:
    """Resolve the effective HTTP status from the transport or provider body."""
    if transport.http_status is not None:
        return transport.http_status
    status = provider_response.get("statusCode")
    if status is None or isinstance(status, bool):
        return None
    try:
        parsed = int(status)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _provider_error(data: Any, provider_response: Mapping[str, Any]) -> str | None:
    """Return an explicit provider failure without misclassifying extracted JSON."""
    for key in ("error", "errorMessage", "error_message"):
        value = provider_response.get(key)
        if value is not None and str(value).strip():
            return redact_error_text(value, max_chars=_MAX_DIAGNOSTIC_CHARS)

    # A top-level ``error`` key can be legitimate user-requested structured
    # extraction. Treat it as provider authority only when the envelope itself
    # explicitly declares failure.
    if isinstance(data, Mapping) and data.get("success") is False:
        for key in ("error", "errorMessage", "message"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return redact_error_text(value, max_chars=_MAX_DIAGNOSTIC_CHARS)
    return None


def _provider_content_type(provider_response: Mapping[str, Any]) -> str | None:
    for key in ("contentType", "content_type", "content-type"):
        value = provider_response.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _request_format(metadata: Mapping[str, Any]) -> str | None:
    request = metadata.get("request")
    if not isinstance(request, Mapping):
        return None
    value = request.get("format")
    return str(value) if value is not None else None


def _provider_content_text(data: Any) -> str | None:
    """Extract non-markdown provider content that still needs suitability checks."""
    if not isinstance(data, Mapping):
        return None
    for key in ("summary", "content", "text", "html", "rawHtml"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    nested = data.get("data")
    if isinstance(nested, Mapping):
        web = nested.get("web")
        if isinstance(web, list) and web and isinstance(web[0], Mapping):
            for key in ("summary", "content", "text", "html", "rawHtml"):
                value = web[0].get(key)
                if isinstance(value, str):
                    return value
    return None


def _usable_payload(transport: ScrapeTransportResult, data: Any) -> bytes:
    """Return the payload form the shared preflight checker understands.

    Firecrawl markdown envelopes are already understood by
    ``CandidatePreflightChecker``. Raw text/HTML/summary output is normalized to
    that envelope. Valid JSON that is merely a provider envelope is *not*
    converted into content: absent markdown/text therefore remains an honest
    ``empty_content`` outcome. User-requested structured extraction formats are
    represented as text only after provider status/error metadata has been
    separated from the content contract.
    """
    if extract_markdown(data) is not None:
        return transport.raw_payload

    content_text = _provider_content_text(data)
    if content_text is not None:
        return json.dumps({"markdown": content_text}).encode("utf-8")

    is_json = _is_json_payload(transport.raw_payload)
    request_format = _request_format(transport.metadata)
    if is_json and request_format not in _STRUCTURED_CONTENT_FORMATS:
        return transport.raw_payload

    text = transport.raw_payload.decode("utf-8", errors="replace")
    return json.dumps({"markdown": text}).encode("utf-8")


def _bounded_provider_response(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not metadata:
        return {}
    redacted = redact_diagnostic_value(dict(metadata))
    if not isinstance(redacted, dict):
        return {}
    encoded = json.dumps(redacted, default=str, sort_keys=True)
    if len(encoded) <= _MAX_PROVIDER_RESPONSE_CHARS:
        return redacted
    return {"truncated": encoded[:_MAX_PROVIDER_RESPONSE_CHARS]}


def assess_scrape_transport_result(
    transport: ScrapeTransportResult,
    checker: CandidatePreflightChecker,
) -> ProviderResponseAssessment:
    """Assess a completed direct-scrape transport against preflight policy.

    The caller must only pass a ``transport`` whose ``succeeded`` property is
    true (exit 0 with a non-empty body). Transport-level failures are owned by
    ``DirectScrapeService`` and never reach this policy.
    """
    if not transport.succeeded:
        raise ValueError(
            "assess_scrape_transport_result requires a succeeded transport"
        )

    data = _parse_payload(transport.raw_payload)
    provider_response = extract_response_metadata(data)
    http_status = _provider_http_status(transport, provider_response)
    provider_error = _provider_error(data, provider_response)
    metadata = dict(transport.metadata)

    content_type = (
        metadata.get("content_type")
        or metadata.get("contentType")
        or _provider_content_type(provider_response)
    )
    if content_type is not None:
        metadata["content_type"] = str(content_type)

    payload = _usable_payload(transport, data)
    adapted = SearchAdapterResult(
        raw_payload=payload,
        http_status=http_status,
        provider_request_id=transport.provider_request_id,
        transport_error=(
            provider_error
            if provider_error is not None
            and (http_status is None or http_status < 400)
            else None
        ),
        transport_metadata=metadata,
    )
    preflight = checker.check(adapted, payload)

    if preflight.classification == "suitable":
        return ProviderResponseAssessment(
            suitable=True,
            transport=(
                replace(transport, http_status=http_status)
                if transport.http_status != http_status
                else transport
            ),
            classification=preflight.classification,
            reason_code=preflight.reason_code,
            reason=redact_error_text(preflight.reason),
            failure_class="none",
        )

    failure_class = _CLASSIFICATION_TO_FAILURE_CLASS.get(
        preflight.classification, "internal"
    )
    diagnostic = redact_error_text(
        f"provider response rejected: {preflight.classification} "
        f"({preflight.reason_code}): {preflight.reason}",
        max_chars=_MAX_DIAGNOSTIC_CHARS,
    )
    enriched = replace(
        transport,
        http_status=http_status,
        stderr=diagnostic.encode("utf-8", errors="replace"),
        metadata={
            **metadata,
            "failure_class": failure_class,
            "provider_response": _bounded_provider_response(provider_response),
        },
    )
    return ProviderResponseAssessment(
        suitable=False,
        transport=enriched,
        classification=preflight.classification,
        reason_code=preflight.reason_code,
        reason=redact_error_text(preflight.reason),
        failure_class=failure_class,
    )
