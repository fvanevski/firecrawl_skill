"""Metadata-only Firecrawl search transport adapter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from ...domain import utcnow
from ..models import SearchAdapterResult

_MAX_DIAGNOSTIC_CHARS = 500
_TRANSIENT_MARKERS = ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")


class MetadataOnlyFirecrawlSearchAdapter:
    """Run Firecrawl search without implicit scrape or filesystem output."""

    def __init__(
        self,
        *,
        executable: str = "firecrawl",
        timeout_seconds: int = 60,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        retries: int = 2,
        **_: Any,
    ) -> SearchAdapterResult:
        if backend != "firecrawl":
            raise ValueError(
                "authoritative fsearch supports only the firecrawl backend"
            )
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        command = [
            self.executable,
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
            command.extend(["--tbs", tbs])

        requested_at = utcnow()
        last_code = 0
        last_stdout = b""
        last_stderr = b""
        responded_at = requested_at
        attempts = 0
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                process = self.runner(
                    command,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_code = 124
                last_stdout = _as_bytes(exc.stdout)
                last_stderr = str(exc).encode("utf-8", errors="replace")
            except OSError as exc:
                last_code = 127
                last_stdout = b""
                last_stderr = str(exc).encode("utf-8", errors="replace")
            else:
                last_code = int(process.returncode)
                last_stdout = _as_bytes(process.stdout)
                last_stderr = _as_bytes(process.stderr)
            responded_at = utcnow()
            if last_code == 0 and last_stdout:
                return SearchAdapterResult(
                    raw_payload=last_stdout,
                    http_status=200,
                    provider_request_id=None,
                    transport_error=None,
                    transport_metadata={
                        "adapter": type(self).__name__,
                        "attempts": attempts,
                        "command": command,
                        "exit_code": last_code,
                        "implicit_scrape": False,
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )
            diagnostic = last_stderr.decode("utf-8", errors="replace")
            transient = any(marker in diagnostic for marker in _TRANSIENT_MARKERS)
            if not transient or attempt >= retries:
                break

        diagnostic = last_stderr.decode("utf-8", errors="replace").strip()
        transport_error = _classify_search_transport_error(
            last_code, diagnostic, bool(last_stdout)
        )
        payload = (
            last_stdout
            if last_code == 0 and last_stdout
            else json.dumps({"success": False, "error": transport_error}).encode(
                "utf-8"
            )
        )
        return SearchAdapterResult(
            raw_payload=payload,
            http_status=500,
            provider_request_id=None,
            transport_error=transport_error,
            transport_metadata={
                "adapter": type(self).__name__,
                "attempts": attempts,
                "command": command,
                "exit_code": last_code,
                "stderr": _bounded_text(diagnostic),
                "implicit_scrape": False,
            },
            requested_at=requested_at,
            responded_at=responded_at,
        )


def _classify_search_transport_error(
    returncode: int, diagnostic: str, has_payload: bool
) -> str:
    for marker in _TRANSIENT_MARKERS:
        if marker in diagnostic:
            return f"Network transport error: {marker}"
    if returncode == 0 and not has_payload:
        return "Firecrawl search returned an empty response"
    if diagnostic:
        return (
            f"Firecrawl search failed (exit {returncode}): {_bounded_text(diagnostic)}"
        )
    return f"Firecrawl search failed with exit code {returncode}"


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else value


def _bounded_text(value: str) -> str:
    return str(value)[:_MAX_DIAGNOSTIC_CHARS]


__all__ = ["MetadataOnlyFirecrawlSearchAdapter"]
