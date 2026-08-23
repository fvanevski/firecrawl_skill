"""Bounded Firecrawl provider adapter for extraction suitability (issue #216).

Search is discovery-only. Candidate extraction is a separate bounded child
operation with first-byte, provider-operation, and overall-candidate deadlines.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ...domain import utcnow
from ...first_byte_retry import FirstByteTimeoutRetryPolicy
from ...provider_preflight import (
    BoundedSubprocessRunner,
    CandidatePreflightChecker,
    CandidatePreflightResult,
    ExtractionDeadlinePolicy,
    ProviderCommandResult,
    extract_markdown,
    extract_response_metadata,
    redact_diagnostic_value,
    redact_error_text,
    validate_candidate_url,
)
from ..models import SearchAdapterResult

_TRANSIENT_TAGS = ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")


class BoundedFirecrawlSearchAdapter:
    """Firecrawl adapter with discovery/extraction separation and hard deadlines."""

    def __init__(
        self,
        runner: Callable[..., tuple[int, bytes, str] | ProviderCommandResult]
        | BoundedSubprocessRunner
        | None = None,
        *,
        deadline_policy: ExtractionDeadlinePolicy | None = None,
        preflight_checker: CandidatePreflightChecker | None = None,
        first_byte_retry_policy: FirstByteTimeoutRetryPolicy | None = None,
    ) -> None:
        self.runner = runner or BoundedSubprocessRunner()
        self.deadline_policy = deadline_policy or ExtractionDeadlinePolicy.from_env()
        self.preflight_checker = preflight_checker or CandidatePreflightChecker(
            max_elapsed_seconds=self.deadline_policy.overall_candidate_timeout_seconds
        )
        self.first_byte_retry_policy = (
            first_byte_retry_policy or FirstByteTimeoutRetryPolicy.from_env()
        )

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        retries: int = 2,
        **_kwargs: Any,
    ) -> SearchAdapterResult:
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")
        if backend == "firecrawl_scrape":
            return self.scrape_url(query_text, transient_retries=retries)

        cmd = [
            "firecrawl",
            "search",
            query_text,
            "--limit",
            str(limit),
            "--sources",
            sources,
            "--ignore-invalid-urls",
            "--json",
        ]
        if tbs:
            cmd.extend(["--tbs", tbs])

        requested_at = utcnow()
        max_retries = min(max(0, retries), self.deadline_policy.transient_retries)
        command_result: ProviderCommandResult | None = None
        for attempt in range(max_retries + 1):
            command_result = self._run_command(
                cmd,
                first_byte_timeout_seconds=self.deadline_policy.first_byte_timeout_seconds,
                operation_timeout_seconds=self.deadline_policy.provider_operation_timeout_seconds,
            )
            if command_result.returncode == 0 and command_result.stdout:
                responded_at = utcnow()
                return SearchAdapterResult(
                    raw_payload=command_result.stdout,
                    http_status=200,
                    provider_request_id=None,
                    transport_error=None,
                    transport_metadata={
                        "attempt": attempt + 1,
                        "attempts": attempt + 1,
                        "operation": "search_discovery",
                        "exit_code": command_result.returncode,
                        "first_byte_seconds": command_result.first_byte_seconds,
                        "provider_operation_seconds": command_result.elapsed_seconds,
                        "elapsed_seconds": (
                            responded_at - requested_at
                        ).total_seconds(),
                        "timeout_reason": command_result.timeout_reason,
                        "cancelled": command_result.cancelled,
                        "deadline_policy": self.deadline_policy.to_dict(),
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )
            if not self._is_transient_command_failure(command_result):
                break

        assert command_result is not None
        responded_at = utcnow()
        transient_tag = next(
            (tag for tag in _TRANSIENT_TAGS if tag in command_result.stderr), None
        )
        error = (
            f"Network transport error: {transient_tag}"
            if transient_tag
            else self._command_error("Firecrawl search", command_result)
        )
        return SearchAdapterResult(
            raw_payload=json.dumps({"success": False, "error": error}).encode(),
            http_status=500,
            provider_request_id=None,
            transport_error=error,
            transport_metadata={
                "attempts": max_retries + 1,
                "operation": "search_discovery",
                "exit_code": command_result.returncode,
                "stderr": redact_error_text(command_result.stderr, max_chars=500),
                "first_byte_seconds": command_result.first_byte_seconds,
                "provider_operation_seconds": command_result.elapsed_seconds,
                "elapsed_seconds": (responded_at - requested_at).total_seconds(),
                "timeout_reason": command_result.timeout_reason,
                "cancelled": command_result.cancelled,
                "deadline_policy": self.deadline_policy.to_dict(),
            },
            requested_at=requested_at,
            responded_at=responded_at,
        )

    def scrape_url(
        self,
        url: str,
        *,
        transient_retries: int | None = None,
    ) -> SearchAdapterResult:
        requested_at = utcnow()
        started = time.monotonic()
        url_rejection = validate_candidate_url(url)
        if url_rejection is not None:
            return self._terminal_result(
                url=url,
                requested_at=requested_at,
                started=started,
                outcome=url_rejection,
                raw_payload=b"{}",
                http_status=None,
                provider_request_id=None,
                command_result=None,
                metadata={},
            )

        cmd = [
            "firecrawl",
            "scrape",
            url,
            "--format",
            "markdown",
            "--only-main-content",
            "--json",
        ]
        max_transient_retries = (
            self.deadline_policy.transient_retries
            if transient_retries is None
            else min(max(0, transient_retries), self.deadline_policy.transient_retries)
        )
        transient_failures = 0
        first_byte_failures = 0
        empty_failures = 0
        attempt = 0
        provider_sub_attempts: list[dict[str, Any]] = []

        while True:
            elapsed_before = time.monotonic() - started
            remaining = (
                self.deadline_policy.overall_candidate_timeout_seconds - elapsed_before
            )
            if remaining <= 0:
                outcome = CandidatePreflightResult(
                    classification="timeout",
                    reason_code="overall_candidate_timeout",
                    reason=(
                        "overall candidate deadline expired before another provider "
                        "operation could begin"
                    ),
                    failure_stage="overall_candidate",
                    elapsed_seconds=elapsed_before,
                    cancelled=True,
                    terminal=True,
                )
                return self._terminal_result(
                    url=url,
                    requested_at=requested_at,
                    started=started,
                    outcome=outcome,
                    raw_payload=b"{}",
                    http_status=None,
                    provider_request_id=None,
                    command_result=None,
                    metadata={},
                    attempts=attempt,
                    provider_sub_attempts=provider_sub_attempts,
                )

            attempt += 1
            command_result = self._run_command(
                cmd,
                first_byte_timeout_seconds=min(
                    self.deadline_policy.first_byte_timeout_seconds, remaining
                ),
                operation_timeout_seconds=min(
                    self.deadline_policy.provider_operation_timeout_seconds, remaining
                ),
            )
            responded_at = utcnow()
            total_elapsed = time.monotonic() - started

            if command_result.returncode == 0 and command_result.stdout:
                try:
                    provider_data = json.loads(command_result.stdout)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    outcome = CandidatePreflightResult(
                        classification="malformed",
                        reason_code="invalid_provider_response",
                        reason=f"invalid Firecrawl scrape JSON: {type(exc).__name__}",
                        failure_stage="provider_response",
                        elapsed_seconds=total_elapsed,
                        first_byte_seconds=command_result.first_byte_seconds,
                        provider_operation_seconds=command_result.elapsed_seconds,
                        cancelled=False,
                        terminal=True,
                    )
                    provider_sub_attempts.append(
                        self._sub_attempt(attempt, command_result, outcome)
                    )
                    return self._terminal_result(
                        url=url,
                        requested_at=requested_at,
                        started=started,
                        outcome=outcome,
                        raw_payload=command_result.stdout,
                        http_status=None,
                        provider_request_id=None,
                        command_result=command_result,
                        metadata={},
                        attempts=attempt,
                        provider_sub_attempts=provider_sub_attempts,
                    )

                metadata = extract_response_metadata(provider_data)
                status = self._status_code(metadata)
                content_type = self._content_type(metadata)
                wrapped = self._wrapped_scrape_payload(url, provider_data, metadata)
                candidate_result = SearchAdapterResult(
                    raw_payload=wrapped,
                    http_status=status,
                    provider_request_id=self._request_id(metadata),
                    transport_error=None,
                    transport_metadata={
                        "attempt": attempt,
                        "attempts": attempt,
                        "operation": "candidate_scrape",
                        "exit_code": command_result.returncode,
                        "first_byte_seconds": command_result.first_byte_seconds,
                        "provider_operation_seconds": command_result.elapsed_seconds,
                        "elapsed_seconds": total_elapsed,
                        "timeout_reason": command_result.timeout_reason,
                        "cancelled": command_result.cancelled,
                        "content_type": content_type,
                        "deadline_policy": self.deadline_policy.to_dict(),
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )
                outcome = self.preflight_checker.check(candidate_result)
            else:
                error = self._command_error("Firecrawl scrape", command_result)
                candidate_result = SearchAdapterResult(
                    raw_payload=json.dumps({"success": False, "error": error}).encode(),
                    http_status=None,
                    provider_request_id=None,
                    transport_error=error,
                    transport_metadata={
                        "attempt": attempt,
                        "attempts": attempt,
                        "operation": "candidate_scrape",
                        "exit_code": command_result.returncode,
                        "stderr": redact_error_text(
                            command_result.stderr, max_chars=500
                        ),
                        "first_byte_seconds": command_result.first_byte_seconds,
                        "provider_operation_seconds": command_result.elapsed_seconds,
                        "elapsed_seconds": total_elapsed,
                        "timeout_reason": command_result.timeout_reason,
                        "cancelled": command_result.cancelled,
                        "deadline_policy": self.deadline_policy.to_dict(),
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )
                outcome = self.preflight_checker.check(candidate_result)

            provider_sub_attempts.append(self._sub_attempt(attempt, command_result, outcome))
            candidate_result = replace(
                candidate_result,
                transport_metadata={
                    **candidate_result.transport_metadata,
                    "attempts": attempt,
                    "provider_sub_attempts": list(provider_sub_attempts),
                    "first_byte_retry_policy": self.first_byte_retry_policy.to_dict(),
                },
            )

            if outcome.classification == "suitable":
                return replace(
                    candidate_result,
                    transport_metadata={
                        **candidate_result.transport_metadata,
                        "preflight": outcome.to_metadata(),
                    },
                )

            if outcome.reason_code == "first_byte_timeout":
                if first_byte_failures < self.first_byte_retry_policy.retries:
                    first_byte_failures += 1
                    continue
                outcome = self._first_byte_exhausted(outcome, attempt)
                return replace(
                    candidate_result,
                    transport_metadata={
                        **candidate_result.transport_metadata,
                        "preflight": outcome.to_metadata(),
                    },
                )

            if outcome.classification == "empty_content":
                if empty_failures < self.deadline_policy.empty_content_retries:
                    empty_failures += 1
                    continue
                return replace(
                    candidate_result,
                    transport_metadata={
                        **candidate_result.transport_metadata,
                        "preflight": outcome.to_metadata(),
                    },
                )

            if outcome.retryable:
                if transient_failures < max_transient_retries:
                    transient_failures += 1
                    continue
                outcome = outcome.with_retry_exhausted()

            return replace(
                candidate_result,
                transport_metadata={
                    **candidate_result.transport_metadata,
                    "preflight": outcome.to_metadata(),
                },
            )

    @staticmethod
    def _sub_attempt(
        attempt: int,
        command_result: ProviderCommandResult,
        outcome: CandidatePreflightResult,
    ) -> dict[str, Any]:
        return {
            "attempt": attempt,
            "classification": outcome.classification,
            "reason_code": outcome.reason_code,
            "exit_code": command_result.returncode,
            "elapsed_seconds": command_result.elapsed_seconds,
            "first_byte_seconds": command_result.first_byte_seconds,
            "timeout_reason": command_result.timeout_reason,
            "cancelled": command_result.cancelled,
        }

    @staticmethod
    def _first_byte_exhausted(
        outcome: CandidatePreflightResult, attempts: int
    ) -> CandidatePreflightResult:
        return replace(
            outcome,
            reason_code="first_byte_timeout",
            reason=(
                "first-byte timeout retry budget exhausted; "
                f"bounded_attempts={attempts}; last_reason={outcome.reason}"
            ),
            retryable=False,
            terminal=True,
        )

    def _terminal_result(
        self,
        *,
        url: str,
        requested_at,
        started: float,
        outcome: CandidatePreflightResult,
        raw_payload: bytes,
        http_status: int | None,
        provider_request_id: str | None,
        command_result: ProviderCommandResult | None,
        metadata: dict[str, Any],
        attempts: int = 0,
        provider_sub_attempts: list[dict[str, Any]] | None = None,
    ) -> SearchAdapterResult:
        responded_at = utcnow()
        total_elapsed = time.monotonic() - started
        normalized = replace(
            outcome,
            elapsed_seconds=(
                outcome.elapsed_seconds
                if outcome.elapsed_seconds is not None
                else total_elapsed
            ),
        )
        transport = {
            "attempts": attempts,
            "operation": "candidate_scrape",
            "deadline_policy": self.deadline_policy.to_dict(),
            "first_byte_retry_policy": self.first_byte_retry_policy.to_dict(),
            "provider_sub_attempts": list(provider_sub_attempts or ()),
            "content_type": self._content_type(metadata),
            "elapsed_seconds": total_elapsed,
            "preflight": normalized.to_metadata(),
        }
        if command_result is not None:
            transport.update(
                {
                    "exit_code": command_result.returncode,
                    "first_byte_seconds": command_result.first_byte_seconds,
                    "provider_operation_seconds": command_result.elapsed_seconds,
                    "timeout_reason": command_result.timeout_reason,
                    "cancelled": command_result.cancelled,
                    "stderr": redact_error_text(command_result.stderr, max_chars=500),
                }
            )
        return SearchAdapterResult(
            raw_payload=raw_payload,
            http_status=http_status,
            provider_request_id=provider_request_id,
            transport_error=None,
            transport_metadata=transport,
            requested_at=requested_at,
            responded_at=responded_at,
        )

    def _run_command(
        self,
        cmd: list[str],
        *,
        first_byte_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> ProviderCommandResult:
        if isinstance(self.runner, BoundedSubprocessRunner):
            return self.runner.run(
                cmd,
                first_byte_timeout_seconds=first_byte_timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            )
        run_method = getattr(self.runner, "run", None)
        if callable(run_method):
            result = run_method(
                cmd,
                first_byte_timeout_seconds=first_byte_timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            )
            if isinstance(result, ProviderCommandResult):
                return result

        started = time.monotonic()
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(self.runner)
            if "timeout" in signature.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values()
            ):
                kwargs["timeout"] = operation_timeout_seconds
        except (TypeError, ValueError):
            pass
        raw_result = self.runner(cmd, **kwargs)  # type: ignore[misc,operator]
        elapsed = time.monotonic() - started
        if isinstance(raw_result, ProviderCommandResult):
            return raw_result
        code, stdout, stderr = raw_result
        first_byte = elapsed if stdout or stderr else None
        timeout_reason = None
        cancelled = False
        if first_byte is None and elapsed > first_byte_timeout_seconds:
            timeout_reason = "first_byte_timeout"
            cancelled = True
        elif elapsed > operation_timeout_seconds:
            timeout_reason = "provider_operation_timeout"
            cancelled = True
        return ProviderCommandResult(
            returncode=int(code),
            stdout=bytes(stdout),
            stderr=str(stderr),
            elapsed_seconds=elapsed,
            first_byte_seconds=first_byte,
            timeout_reason=timeout_reason,
            cancelled=cancelled,
        )

    @staticmethod
    def _is_transient_command_failure(result: ProviderCommandResult) -> bool:
        return any(tag in result.stderr for tag in _TRANSIENT_TAGS)

    @staticmethod
    def _command_error(label: str, result: ProviderCommandResult) -> str:
        diagnostic = redact_error_text(result.stderr.strip(), max_chars=300)
        if diagnostic:
            return f"{label} failed (exit {result.returncode}): {diagnostic}"
        return f"{label} failed with exit code {result.returncode}"

    @staticmethod
    def _status_code(metadata: dict[str, Any]) -> int | None:
        value = metadata.get("statusCode") or metadata.get("status_code")
        try:
            return int(value) if value is not None else 200
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _content_type(metadata: dict[str, Any]) -> str | None:
        for key in ("contentType", "content_type", "mimeType", "mime_type"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _request_id(metadata: dict[str, Any]) -> str | None:
        for key in ("scrapeId", "scrape_id", "requestId", "request_id"):
            value = metadata.get(key)
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _wrapped_scrape_payload(
        url: str, provider_data: Any, metadata: dict[str, Any]
    ) -> bytes:
        markdown = extract_markdown(provider_data)
        payload = {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": metadata.get("url") or metadata.get("sourceURL") or url,
                        "title": metadata.get("title") or url.rsplit("/", 1)[-1],
                        "description": metadata.get("description") or "",
                        "markdown": markdown,
                        "metadata": redact_diagnostic_value(metadata),
                    }
                ]
            },
        }
        return json.dumps(payload).encode("utf-8")


__all__ = ["BoundedFirecrawlSearchAdapter"]
