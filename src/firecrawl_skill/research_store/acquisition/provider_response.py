"""Provider response suitability assessment for authoritative direct scrapes.

A completed transport (exit 0, non-empty body) is not necessarily a usable
acquisition. The provider can return a non-2xx HTTP status, an anti-bot
interstitial, or an empty/unsupported body while the Firecrawl CLI still
exits cleanly. This module adapts a ``ScrapeTransportResult`` to the shared
candidate-preflight policy and reports a single deterministic assessment that
the direct-scrape application service uses to decide authoritative success
versus failure persistence.

The assessment only classifies provider-side rejections. It never mutates the
raw payload or invents provenance; it reuses the existing preflight checker
and the public response-metadata extractor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from ..domain import SearchAdapterResult
from ..provider_preflight import (
    CandidatePreflightChecker,
    extract_markdown,
    extract_response_metadata,
    redact_error_text,
)
from .models import ScrapeTransportResult

_MAX_PROVIDER_RESPONSE_CHARS = 1000
_MAX_DIAGNOSTIC_CHARS = 500

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


def _provider_http_status(transport: ScrapeTransportResult) -> int | None:
    """Resolve the effective HTTP status from the transport or provider body."""
    if transport.http_status is not None:
        return transport.http_status
    metadata = extract_response_metadata(_parse_payload(transport.raw_payload))
    status = metadata.get("statusCode")
    if isinstance(status, int) and status > 0:
        return status
    return None


def _usable_payload(transport: ScrapeTransportResult) -> bytes:
    """Return the payload form the shared preflight checker understands.

    Firecrawl's markdown and summary contracts emit raw content on stdout, while
    the structured/JSON contracts emit an API envelope. Both are represented to
    the shared checker as the envelope shape ``extract_markdown`` understands,
    so raw content is normalized into a single-field envelope rather than being
    misread as a missing-content response.
    """
    if extract_markdown(_parse_payload(transport.raw_payload)) is not None:
        return transport.raw_payload
    text = transport.raw_payload.decode("utf-8", errors="replace")
    return json.dumps({"markdown": text}).encode("utf-8")


def _bounded_provider_response(metadata: dict[str, Any]) -> dict[str, Any]:
    if not metadata:
        return {}
    encoded = json.dumps(metadata, default=str, sort_keys=True)
    if len(encoded) <= _MAX_PROVIDER_RESPONSE_CHARS:
        return dict(metadata)
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

    http_status = _provider_http_status(transport)
    metadata = dict(transport.metadata)
    payload = _usable_payload(transport)
    provider_response = _bounded_provider_response(
        extract_response_metadata(_parse_payload(transport.raw_payload))
    )
    adapted = SearchAdapterResult(
        raw_payload=payload,
        http_status=http_status,
        provider_request_id=transport.provider_request_id,
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
            "provider_response": provider_response,
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
