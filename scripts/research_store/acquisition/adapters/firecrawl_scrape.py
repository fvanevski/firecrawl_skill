"""Direct Firecrawl scrape transport adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ...domain import utcnow
from ..models import DIRECT_SCRAPE_SUPPORTED_FORMATS, ScrapeTransportResult

_FIRECRAWL_ADAPTER_VERSION = "direct-v2"
_FIRECRAWL_STDOUT_CONTRACT = "single-format-raw-stdout-v1"


class FirecrawlDirectScrapeAdapter:
    """Capture Firecrawl CLI output through pipes rather than output files."""

    def __init__(
        self,
        *,
        executable: str = "firecrawl",
        timeout_seconds: int = 60,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        version_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.version_runner = version_runner
        self._version_cache: str | None = None

    def scrape(
        self,
        url: str,
        *,
        format: str = "markdown",
        summary: bool = False,
        schema: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ScrapeTransportResult:
        if format not in DIRECT_SCRAPE_SUPPORTED_FORMATS:
            raise ValueError(f"unsupported scrape format: {format}")
        effective_format = (
            "json" if schema is not None else ("summary" if summary else format)
        )
        command = [self.executable, "scrape", url, "--format", effective_format]
        if schema is not None:
            command.extend(
                [
                    "--schema",
                    json.dumps(schema, sort_keys=True, separators=(",", ":")),
                ]
            )
        elif effective_format == "markdown":
            command.extend(
                [
                    "--only-main-content",
                    "--exclude-tags",
                    (
                        "nav,footer,aside,header,script,style,.sidebar,#sidebar,"
                        ".ad,.menu,#menu,.header,.footer,#header,#footer,#nav,.nav"
                    ),
                ]
            )
        self._append_options(command, options or {})

        requested_at = utcnow()
        try:
            process = self.runner(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = str(exc).encode("utf-8", errors="replace")
            return ScrapeTransportResult(
                raw_payload=exc.stdout or b"",
                returncode=124,
                stderr=stderr,
                requested_at=requested_at,
                responded_at=utcnow(),
                metadata={"failure_class": "timeout"},
            )
        except OSError as exc:
            return ScrapeTransportResult(
                raw_payload=b"",
                returncode=127,
                stderr=str(exc).encode("utf-8", errors="replace"),
                requested_at=requested_at,
                responded_at=utcnow(),
                metadata={"failure_class": "network"},
            )

        stdout = (
            process.stdout.encode()
            if isinstance(process.stdout, str)
            else process.stdout
        )
        stderr = (
            process.stderr.encode()
            if isinstance(process.stderr, str)
            else process.stderr
        )
        return ScrapeTransportResult(
            raw_payload=stdout or b"",
            returncode=int(process.returncode),
            stderr=stderr or b"",
            requested_at=requested_at,
            responded_at=utcnow(),
            metadata={
                "adapter": type(self).__name__,
                "adapter_version": _FIRECRAWL_ADAPTER_VERSION,
                "stdout_contract": _FIRECRAWL_STDOUT_CONTRACT,
                "firecrawl_cli_version": self._cli_version(),
                "command": self._sanitized_command(command),
                "request": {
                    "format": effective_format,
                    "schema": schema is not None,
                    "options": dict(options or {}),
                },
                "exit_code": int(process.returncode),
            },
        )

    def _cli_version(self) -> str | None:
        if self._version_cache is not None:
            return self._version_cache
        try:
            process = self.version_runner(
                [self.executable, "--version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            value = process.stdout
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            version = str(value or "").strip()[:100]
            self._version_cache = version or "unknown"
        except (OSError, subprocess.SubprocessError):
            self._version_cache = "unknown"
        return self._version_cache

    @staticmethod
    def _sanitized_command(command: Sequence[str]) -> list[str]:
        sanitized = list(command)
        if "--schema" in sanitized:
            index = sanitized.index("--schema") + 1
            if index < len(sanitized):
                digest = hashlib.sha256(sanitized[index].encode()).hexdigest()[:12]
                sanitized[index] = f"<schema-sha256:{digest}>"
        return sanitized

    @staticmethod
    def _append_options(command: list[str], options: Mapping[str, Any]) -> None:
        supported = {
            "include_tags": "--include-tags",
            "exclude_tags": "--exclude-tags",
            "wait_for": "--wait-for",
            "timeout": "--timeout",
            "mobile": "--mobile",
            "location": "--location",
        }
        unknown = sorted(set(options) - set(supported))
        if unknown:
            raise ValueError(f"unsupported Firecrawl options: {', '.join(unknown)}")
        for key in sorted(options):
            value = options[key]
            flag = supported[key]
            if isinstance(value, bool):
                if value:
                    command.append(flag)
            elif value is not None:
                command.extend([flag, str(value)])


__all__ = ["FirecrawlDirectScrapeAdapter"]
