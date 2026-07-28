"""Token accounting for semantic calls.

This module provides:

* ``TokenAccountant`` — extracts token counts from endpoint response
  metadata, falls back to tokenizer-based counting when endpoint usage
  is absent.
* ``extract_endpoint_usage`` — parses an OpenAI-compatible response to
  extract prompt_tokens, completion_tokens, total_tokens.
* ``TokenizerBackedAccountant`` — counts tokens using a configured
  tokenizer on the stored request/response payload.

## Authoritative state

- Token counts are stored in ``endpoint_usage_records`` (via the
  telemetry service) and aggregated in ``run_performance_telemetry``.
- When endpoint response provides usage, ``source = "endpoint"``.
- When endpoint response lacks usage but the stored request/response
  is available with a configured tokenizer, ``source = "tokenizer"``.
- When neither is available, ``source = "unavailable"``.

## Invariants

1. Endpoint usage takes priority over tokenizer counting.
2. Tokenizer counting is only used when the endpoint response does not
   include a ``usage`` field, or the ``usage`` field is empty.
3. Tokenizer-based counts are explicitly marked ``source = "tokenizer"``
   so consumers know they are derived, not from the endpoint.
4. When both endpoint and tokenizer are unavailable, the total is
   ``None`` and the status is ``unavailable`` — never zero.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

try:
    import tiktoken

    _HAS_TIKTOKEN = True
except ImportError:
    tiktoken = None  # type: ignore[assignment,misc]
    _HAS_TIKTOKEN = False

from research_domain.models import TokenAccounting

logger = logging.getLogger(__name__)


def extract_endpoint_usage(
    response_metadata: Mapping[str, Any],
) -> TokenAccounting:
    """Extract token usage from an OpenAI-compatible response metadata.

    The OpenAI-compatible API (vLLM, OpenAI, etc.) returns a ``usage``
    field in the response. This function looks for it in the response
    metadata and extracts prompt_tokens, completion_tokens, and total_tokens.

    Args:
        response_metadata: The response metadata from a semantic call.
            Expected structure:
            {
                "provenance": {...},
                "attempts": [...],
                "attempt_count": N,
                "usage": {
                    "prompt_tokens": N,
                    "completion_tokens": N,
                    "total_tokens": N
                }
            }

    Returns:
        A TokenAccounting with source="endpoint" when usage is found,
        or source="unavailable" when not.
    """
    # Look for usage in response_metadata directly.
    usage = (
        response_metadata.get("usage") if isinstance(response_metadata, dict) else None
    )
    if usage is None:
        # Also check inside "provenance" or "attempts".
        provenance = response_metadata.get("provenance", {})
        if isinstance(provenance, dict):
            usage = provenance.get("usage")
        if usage is None:
            attempts = response_metadata.get("attempts", [])
            if isinstance(attempts, list) and attempts:
                # Use the last attempt's usage.
                last_attempt = attempts[-1]
                if isinstance(last_attempt, dict):
                    usage = last_attempt.get("usage")

    if usage is None or not isinstance(usage, dict):
        return TokenAccounting(source="unavailable")

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")

    # Validate: at least one must be present and numeric.
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return TokenAccounting(source="unavailable")

    # Convert to int if numeric (but not bool).
    def _to_int(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        return None

    prompt_tokens = _to_int(prompt_tokens)
    completion_tokens = _to_int(completion_tokens)
    total_tokens = _to_int(total_tokens)

    # If all fields are None after conversion, return unavailable.
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return TokenAccounting(source="unavailable")

    return TokenAccounting(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        source="endpoint",
    )


class TokenizerBackedAccountant:
    """Count tokens using a configured tokenizer.

    This is used as a fallback when the endpoint response does not
    provide usage information.

    Args:
        encoding_name: Name of the tiktoken encoding (e.g. ``"cl100k_base"``).
        prompt_template: Optional format string for the prompt template.
            Used to count prompt tokens from the structured input.
        response_template: Optional format string for the response template.
            Used to count completion tokens from the output.
    """

    def __init__(
        self,
        encoding_name: str = "cl100k_base",
    ) -> None:
        if not _HAS_TIKTOKEN:
            raise RuntimeError(
                "tiktoken is required for tokenizer-backed token accounting. "
                "Install with: pip install tiktoken"
            )
        self.encoding_name = encoding_name
        try:
            self.encoding = tiktoken.get_encoding(encoding_name)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load tiktoken encoding '{encoding_name}': {exc}"
            ) from exc

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string.

        Args:
            text: The text to count tokens for.

        Returns:
            Number of tokens.
        """
        return len(self.encoding.encode(text))

    def account_from_payload(
        self,
        request: Mapping[str, Any],
        response_metadata: Mapping[str, Any],
    ) -> TokenAccounting:
        """Count tokens from stored request and response payloads.

        Args:
            request: The stored request payload (from ``semantic_calls.request``).
            response_metadata: The stored response metadata
                (from ``semantic_calls.response_metadata``).

        Returns:
            A TokenAccounting with source="tokenizer".
        """
        # Extract prompt text from request.
        prompt_text = self._extract_prompt_text(request)
        prompt_tokens = self.count_tokens(prompt_text) if prompt_text else 0

        # Extract response text from response_metadata.
        response_text = self._extract_response_text(response_metadata)
        completion_tokens = self.count_tokens(response_text) if response_text else 0

        return TokenAccounting(
            tokenizer_prompt_tokens=prompt_tokens,
            tokenizer_completion_tokens=completion_tokens,
            tokenizer_total_tokens=prompt_tokens + completion_tokens,
            source="tokenizer",
        )

    def _extract_prompt_text(self, request: Mapping[str, Any]) -> str:
        """Extract prompt text from a request payload."""
        # OpenAI-compatible format: request has "messages" list.
        messages = request.get("messages", [])
        if isinstance(messages, list):
            parts: list[str] = []
            for msg in messages:
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str) and content:
                        parts.append(f"{role}: {content}")
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
                                if text:
                                    parts.append(f"{role}: {text}")
            return "\n".join(parts)
        return ""

    def _extract_response_text(self, response_metadata: Mapping[str, Any]) -> str:
        """Extract response text from response metadata."""
        # Look for the actual response content in provenance or attempts.
        provenance = response_metadata.get("provenance", {})
        if isinstance(provenance, dict):
            choices = provenance.get("choices", [])
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message", {})
                    if isinstance(message, dict):
                        content = message.get("content", "")
                        if isinstance(content, str) and content:
                            return content

        # Fallback: check attempts.
        attempts = response_metadata.get("attempts", [])
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            if isinstance(last, dict):
                content = last.get("content", "")
                if isinstance(content, str) and content:
                    return content
                provenance = last.get("provenance", {})
                if isinstance(provenance, dict):
                    choices = provenance.get("choices", [])
                    if isinstance(choices, list) and choices:
                        first = choices[0]
                        if isinstance(first, dict):
                            message = first.get("message", {})
                            if isinstance(message, dict):
                                content = message.get("content", "")
                                if isinstance(content, str) and content:
                                    return content
        return ""


def account_semantic_call(
    response_metadata: Mapping[str, Any],
    request: Mapping[str, Any] | None = None,
    tokenizer: TokenizerBackedAccountant | None = None,
) -> TokenAccounting:
    """Account tokens for a single semantic call.

    Priority order:

    1. Extract from endpoint response metadata (``usage`` field).
    2. Fall back to tokenizer-based counting when a tokenizer is provided
       and the request payload is available.
    3. Return ``source="unavailable"`` when neither is available.

    Args:
        response_metadata: The response metadata from the semantic call.
        request: The stored request payload (optional, needed for tokenizer).
        tokenizer: Tokenizer-backed accountant (optional, fallback).

    Returns:
        A TokenAccounting with the appropriate source.
    """
    # Priority 1: endpoint usage.
    accounting = extract_endpoint_usage(response_metadata)
    if accounting.source != "unavailable":
        return accounting

    # Priority 2: tokenizer fallback.
    if tokenizer is not None and request is not None:
        try:
            return tokenizer.account_from_payload(request, response_metadata)
        except Exception as exc:
            logger.warning("Tokenizer-backed accounting failed: %s", exc, exc_info=True)

    # Priority 3: unavailable.
    return TokenAccounting(source="unavailable")
