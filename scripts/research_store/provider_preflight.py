from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .domain import SearchAdapterResult

_TRANSIENT_TAGS = ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/markdown",
        "text/plain",
        "application/xhtml+xml",
        "application/json",
        "application/pdf",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|authorization|password|secret|credential)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ANTI_BOT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"verify\s+you\s+are\s+human",
        r"complete\s+(?:the\s+)?captcha",
        r"checking\s+your\s+browser",
        r"are\s+you\s+a\s+robot",
        r"(?:cloudflare|hcaptcha|recaptcha|turnstile).{0,100}(?:challenge|verification|security\s+check|blocked|ray\s+id)",
        r"(?:challenge|verification|security\s+check|blocked|ray\s+id).{0,100}(?:cloudflare|hcaptcha|recaptcha|turnstile)",
        r"ddos\s+protection.{0,100}(?:checking|challenge|verification)",
        r"access\s+denied.{0,100}(?:bot|automated|security\s+check)",
    )
)


def redact_error_text(value: object, *, max_chars: int = 1000) -> str:
    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text[:max_chars]


def redact_diagnostic_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_error_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic_value(item) for item in value]
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class ExtractionDeadlinePolicy:
    """Bounded candidate extraction policy for issue #216."""

    first_byte_timeout_seconds: float = 10.0
    provider_operation_timeout_seconds: float = 30.0
    overall_candidate_timeout_seconds: float = 35.0
    transient_retries: int = 2
    empty_content_retries: int = 0

    def __post_init__(self) -> None:
        if self.first_byte_timeout_seconds <= 0:
            raise ValueError("first-byte timeout must be positive")
        if self.provider_operation_timeout_seconds <= 0:
            raise ValueError("provider-operation timeout must be positive")
        if self.overall_candidate_timeout_seconds <= 0:
            raise ValueError("overall-candidate timeout must be positive")
        if self.transient_retries < 0 or self.empty_content_retries < 0:
            raise ValueError("retry counts must be non-negative")

    @classmethod
    def from_env(cls) -> ExtractionDeadlinePolicy:
        return cls(
            first_byte_timeout_seconds=_env_float(
                "FIRECRAWL_EXTRACTION_FIRST_BYTE_TIMEOUT_SECONDS", 10.0
            ),
            provider_operation_timeout_seconds=_env_float(
                "FIRECRAWL_EXTRACTION_PROVIDER_TIMEOUT_SECONDS", 30.0
            ),
            overall_candidate_timeout_seconds=_env_float(
                "FIRECRAWL_EXTRACTION_CANDIDATE_TIMEOUT_SECONDS", 35.0
            ),
            transient_retries=_env_int(
                "FIRECRAWL_EXTRACTION_TRANSIENT_RETRIES", 2
            ),
            empty_content_retries=_env_int(
                "FIRECRAWL_EXTRACTION_EMPTY_RETRIES", 0
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_byte_timeout_seconds": self.first_byte_timeout_seconds,
            "provider_operation_timeout_seconds": self.provider_operation_timeout_seconds,
            "overall_candidate_timeout_seconds": self.overall_candidate_timeout_seconds,
            "transient_retries": self.transient_retries,
            "empty_content_retries": self.empty_content_retries,
        }


@dataclass(frozen=True)
class ProviderCommandResult:
    returncode: int
    stdout: bytes
    stderr: str
    elapsed_seconds: float
    first_byte_seconds: float | None
    timeout_reason: str | None = None
    cancelled: bool = False


class BoundedSubprocessRunner:
    """Run one provider CLI child with first-byte and operation deadlines."""

    def run(
        self,
        cmd: list[str],
        *,
        first_byte_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> ProviderCommandResult:
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderCommandResult(
                returncode=-1,
                stdout=b"",
                stderr=f"Transport error: {type(exc).__name__}: {exc}",
                elapsed_seconds=time.monotonic() - started,
                first_byte_seconds=None,
            )

        assert process.stdout is not None
        assert process.stderr is not None
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        first_byte_seconds: float | None = None
        timeout_reason: str | None = None
        cancelled = False

        try:
            while True:
                now = time.monotonic()
                elapsed = now - started
                if first_byte_seconds is None and elapsed >= first_byte_timeout_seconds:
                    timeout_reason = "first_byte_timeout"
                    cancelled = True
                    break
                if elapsed >= operation_timeout_seconds:
                    timeout_reason = "provider_operation_timeout"
                    cancelled = True
                    break

                if process.poll() is not None and not selector.get_map():
                    break

                remaining_first = (
                    first_byte_timeout_seconds - elapsed
                    if first_byte_seconds is None
                    else operation_timeout_seconds - elapsed
                )
                wait_for = max(
                    0.0,
                    min(0.1, operation_timeout_seconds - elapsed, remaining_first),
                )
                events = selector.select(wait_for)
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        with suppress(Exception):
                            selector.unregister(key.fileobj)
                        continue
                    if first_byte_seconds is None:
                        first_byte_seconds = time.monotonic() - started
                    if key.data == "stdout":
                        stdout_parts.append(chunk)
                    else:
                        stderr_parts.append(chunk)

                if process.poll() is not None:
                    for stream, target in (
                        (process.stdout, stdout_parts),
                        (process.stderr, stderr_parts),
                    ):
                        while True:
                            try:
                                chunk = os.read(stream.fileno(), 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                with suppress(Exception):
                                    selector.unregister(stream)
                                break
                            if first_byte_seconds is None:
                                first_byte_seconds = time.monotonic() - started
                            target.append(chunk)
                    if not selector.get_map():
                        break

            if cancelled:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1.0)
            else:
                process.wait(timeout=max(0.1, operation_timeout_seconds))
        finally:
            selector.close()

        elapsed_seconds = time.monotonic() - started
        stderr_text = b"".join(stderr_parts).decode("utf-8", errors="replace")
        if timeout_reason:
            marker = (
                "ETIMEDOUT: first byte deadline exceeded"
                if timeout_reason == "first_byte_timeout"
                else "ETIMEDOUT: provider operation deadline exceeded"
            )
            stderr_text = f"{marker}; {stderr_text}" if stderr_text else marker

        return ProviderCommandResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=b"".join(stdout_parts),
            stderr=stderr_text,
            elapsed_seconds=elapsed_seconds,
            first_byte_seconds=first_byte_seconds,
            timeout_reason=timeout_reason,
            cancelled=cancelled,
        )


@dataclass(frozen=True)
class CandidatePreflightResult:
    classification: str
    reason_code: str
    reason: str
    failure_stage: str
    http_status: int | None = None
    content_type: str | None = None
    elapsed_seconds: float | None = None
    first_byte_seconds: float | None = None
    provider_operation_seconds: float | None = None
    cancelled: bool = False
    retryable: bool = False
    terminal: bool = True

    @property
    def is_hard_rejection(self) -> bool:
        return self.classification in {
            "unsuitable_url",
            "empty_content",
            "anti_bot",
            "unsupported_content_type",
            "http_error",
            "timeout",
            "provider_error",
            "malformed",
        }

    @property
    def is_terminal(self) -> bool:
        return self.terminal

    def with_retry_exhausted(self) -> CandidatePreflightResult:
        if not self.retryable:
            return self
        return CandidatePreflightResult(
            classification=self.classification,
            reason_code="transient_retries_exhausted",
            reason=(
                f"transient provider retries exhausted after bounded attempts; "
                f"last_reason={self.reason_code}: {self.reason}"
            ),
            failure_stage=self.failure_stage,
            http_status=self.http_status,
            content_type=self.content_type,
            elapsed_seconds=self.elapsed_seconds,
            first_byte_seconds=self.first_byte_seconds,
            provider_operation_seconds=self.provider_operation_seconds,
            cancelled=False,
            retryable=False,
            terminal=True,
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "reason_code": self.reason_code,
            "reason": redact_error_text(self.reason),
            "failure_stage": self.failure_stage,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "elapsed_seconds": self.elapsed_seconds,
            "first_byte_seconds": self.first_byte_seconds,
            "provider_operation_seconds": self.provider_operation_seconds,
            "cancelled": self.cancelled,
            "retryable": self.retryable,
            "terminal": self.terminal,
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> CandidatePreflightResult:
        return cls(
            classification=str(value["classification"]),
            reason_code=str(value.get("reason_code") or "preflight_unknown"),
            reason=redact_error_text(value.get("reason") or "preflight outcome"),
            failure_stage=str(value.get("failure_stage") or "candidate_preflight"),
            http_status=_optional_int(value.get("http_status")),
            content_type=_optional_str(value.get("content_type")),
            elapsed_seconds=_optional_float(value.get("elapsed_seconds")),
            first_byte_seconds=_optional_float(value.get("first_byte_seconds")),
            provider_operation_seconds=_optional_float(
                value.get("provider_operation_seconds")
            ),
            cancelled=bool(value.get("cancelled", False)),
            retryable=bool(value.get("retryable", False)),
            terminal=bool(value.get("terminal", True)),
        )


class CandidatePreflightChecker:
    """Classify one candidate extraction result without inventing provenance."""

    def __init__(
        self,
        *,
        max_elapsed_seconds: float | None = None,
        min_markdown_length: int = 1,
        anti_bot_patterns: tuple[re.Pattern[str], ...]
        | list[re.Pattern[str]]
        | None = None,
    ) -> None:
        if max_elapsed_seconds is not None and max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")
        if min_markdown_length < 0:
            raise ValueError("min_markdown_length must be non-negative")
        self.max_elapsed_seconds = max_elapsed_seconds
        self.min_markdown_length = min_markdown_length
        self._anti_bot_patterns = tuple(anti_bot_patterns or _ANTI_BOT_PATTERNS)

    def check(
        self,
        result: SearchAdapterResult,
        raw_payload_bytes: bytes | None = None,
    ) -> CandidatePreflightResult:
        transport_metadata = result.transport_metadata or {}
        metadata = transport_metadata.get("preflight")
        if isinstance(metadata, Mapping):
            return CandidatePreflightResult.from_metadata(metadata)

        elapsed = _optional_float(
            getattr(result, "elapsed_seconds", None)
            or transport_metadata.get("elapsed_seconds")
        )
        first_byte = _optional_float(transport_metadata.get("first_byte_seconds"))
        operation = (
            _optional_float(transport_metadata.get("provider_operation_seconds"))
            or elapsed
        )
        content_type = _optional_str(transport_metadata.get("content_type"))
        timeout_reason = transport_metadata.get("timeout_reason")
        if timeout_reason in {"first_byte_timeout", "provider_operation_timeout"}:
            stage = (
                "first_byte"
                if timeout_reason == "first_byte_timeout"
                else "provider_operation"
            )
            return CandidatePreflightResult(
                classification="timeout",
                reason_code=str(timeout_reason),
                reason=(
                    f"{stage} deadline exceeded; elapsed_seconds={elapsed!r}; "
                    f"operation_seconds={operation!r}"
                ),
                failure_stage=stage,
                http_status=result.http_status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                retryable=False,
                terminal=True,
            )
        if (
            self.max_elapsed_seconds is not None
            and elapsed is not None
            and elapsed > self.max_elapsed_seconds
        ):
            return CandidatePreflightResult(
                classification="timeout",
                reason_code="overall_candidate_timeout",
                reason=(
                    f"overall candidate deadline exceeded: {elapsed:.3f}s > "
                    f"{self.max_elapsed_seconds:.3f}s"
                ),
                failure_stage="overall_candidate",
                http_status=result.http_status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                retryable=False,
                terminal=True,
            )

        if result.transport_error:
            safe_error = redact_error_text(result.transport_error, max_chars=300)
            is_transient = any(tag in result.transport_error for tag in _TRANSIENT_TAGS)
            return CandidatePreflightResult(
                classification="transient" if is_transient else "provider_error",
                reason_code=(
                    "transient_transport_error"
                    if is_transient
                    else "provider_transport_error"
                ),
                reason=safe_error,
                failure_stage="provider_transport",
                http_status=result.http_status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=False,
                retryable=is_transient,
                terminal=not is_transient,
            )

        status = result.http_status
        if status is not None and status >= 400:
            if status in _TRANSIENT_HTTP_STATUSES:
                return CandidatePreflightResult(
                    classification="transient",
                    reason_code="transient_http_status",
                    reason=f"transient HTTP status {status}",
                    failure_stage="response_status",
                    http_status=status,
                    content_type=content_type,
                    elapsed_seconds=elapsed,
                    first_byte_seconds=first_byte,
                    provider_operation_seconds=operation,
                    retryable=True,
                    terminal=False,
                )
            return CandidatePreflightResult(
                classification="http_error",
                reason_code="http_error",
                reason=f"HTTP status {status} is not extraction-suitable",
                failure_stage="response_status",
                http_status=status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                terminal=True,
            )

        content_type = _normalize_content_type(content_type)
        if content_type is not None and not _is_supported_content_type(content_type):
            return CandidatePreflightResult(
                classification="unsupported_content_type",
                reason_code="unsupported_content_type",
                reason=f"unsupported content type: {content_type}",
                failure_stage="content_type",
                http_status=status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                terminal=True,
            )

        payload = raw_payload_bytes if raw_payload_bytes is not None else result.raw_payload
        data = _parse_json(payload)
        markdown = extract_markdown(data)
        if markdown is None:
            snippet = extract_text_snippet(data, payload)
            if self._detect_anti_bot(snippet):
                return CandidatePreflightResult(
                    classification="anti_bot",
                    reason_code="anti_bot_interstitial",
                    reason="challenge/interstitial signatures detected without usable content",
                    failure_stage="content_suitability",
                    http_status=status,
                    content_type=content_type,
                    elapsed_seconds=elapsed,
                    first_byte_seconds=first_byte,
                    provider_operation_seconds=operation,
                    cancelled=True,
                    terminal=True,
                )
            return CandidatePreflightResult(
                classification="empty_content",
                reason_code="missing_usable_content",
                reason="provider response contains no usable markdown/text",
                failure_stage="content_suitability",
                http_status=status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                terminal=True,
            )

        stripped = markdown.strip()
        if not stripped or len(stripped) < self.min_markdown_length:
            reason_code = "empty_markdown" if not stripped else "content_too_short"
            return CandidatePreflightResult(
                classification="empty_content",
                reason_code=reason_code,
                reason=(
                    "markdown is empty after stripping"
                    if not stripped
                    else (
                        f"markdown length {len(stripped)} is below minimum "
                        f"{self.min_markdown_length}"
                    )
                ),
                failure_stage="content_suitability",
                http_status=status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                terminal=True,
            )

        if self._detect_anti_bot(stripped):
            return CandidatePreflightResult(
                classification="anti_bot",
                reason_code="anti_bot_interstitial",
                reason="challenge/interstitial signatures detected in extracted content",
                failure_stage="content_suitability",
                http_status=status,
                content_type=content_type,
                elapsed_seconds=elapsed,
                first_byte_seconds=first_byte,
                provider_operation_seconds=operation,
                cancelled=True,
                terminal=True,
            )

        return CandidatePreflightResult(
            classification="suitable",
            reason_code="suitable",
            reason="candidate passed bounded extraction suitability checks",
            failure_stage="content_suitability",
            http_status=status,
            content_type=content_type,
            elapsed_seconds=elapsed,
            first_byte_seconds=first_byte,
            provider_operation_seconds=operation,
            cancelled=False,
            retryable=False,
            terminal=False,
        )

    def _detect_anti_bot(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._anti_bot_patterns)


def validate_candidate_url(url: str) -> CandidatePreflightResult | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return CandidatePreflightResult(
            classification="unsuitable_url",
            reason_code="unsupported_candidate_url",
            reason="candidate URL must be absolute HTTP(S)",
            failure_stage="url_suitability",
            cancelled=True,
            terminal=True,
        )
    return None


def iter_search_items(payload: bytes) -> list[dict[str, Any]]:
    data = _parse_json(payload)
    if not isinstance(data, dict):
        return []
    items = data.get("data")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(items, dict):
        collected: list[dict[str, Any]] = []
        for value in items.values():
            if isinstance(value, list):
                collected.extend(item for item in value if isinstance(item, dict))
        return collected
    return []


def extract_markdown(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    markdown = data.get("markdown")
    if isinstance(markdown, str):
        return markdown
    nested = data.get("data")
    if isinstance(nested, dict):
        web = nested.get("web")
        if isinstance(web, list) and web and isinstance(web[0], dict):
            markdown = web[0].get("markdown")
            if isinstance(markdown, str):
                return markdown
    return None


def extract_response_metadata(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    nested = data.get("data")
    if isinstance(nested, dict):
        web = nested.get("web")
        if isinstance(web, list) and web and isinstance(web[0], dict):
            metadata = web[0].get("metadata")
            if isinstance(metadata, dict):
                return dict(metadata)
    return {}


def extract_text_snippet(data: Any, raw: bytes) -> str:
    if isinstance(data, dict):
        snippets: list[str] = []
        for key in ("title", "description", "snippet", "text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                snippets.append(value.strip())
        nested = data.get("data")
        if isinstance(nested, dict):
            web = nested.get("web")
            if isinstance(web, list) and web and isinstance(web[0], dict):
                for key in ("title", "description", "snippet", "text", "content"):
                    value = web[0].get(key)
                    if isinstance(value, str) and value.strip():
                        snippets.append(value.strip())
        if snippets:
            return " ".join(snippets)[:1000]
    return raw.decode("utf-8", errors="replace")[:1000] if raw else ""


def _parse_json(payload: bytes | str | Any) -> Any:
    if isinstance(payload, bytes):
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload


def _normalize_content_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _is_supported_content_type(value: str) -> bool:
    return value in _ALLOWED_CONTENT_TYPES or value.startswith("text/")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "BoundedSubprocessRunner",
    "CandidatePreflightChecker",
    "CandidatePreflightResult",
    "ExtractionDeadlinePolicy",
    "ProviderCommandResult",
    "extract_markdown",
    "extract_response_metadata",
    "iter_search_items",
    "redact_diagnostic_value",
    "redact_error_text",
    "validate_candidate_url",
]
