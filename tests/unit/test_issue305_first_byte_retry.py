from __future__ import annotations

import json
import os
import time
from typing import cast

import pytest

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.first_byte_retry import (
    FIRST_BYTE_TIMEOUT_RETRIES_ENV,
    FirstByteTimeoutRetryPolicy,
)
from firecrawl_skill.research_store.provider_preflight import (
    BoundedSubprocessRunner,
    ExtractionDeadlinePolicy,
    ProviderCommandResult,
)


def _timeout() -> ProviderCommandResult:
    return ProviderCommandResult(
        returncode=-15,
        stdout=b"",
        stderr="ETIMEDOUT: first byte deadline exceeded",
        elapsed_seconds=0.25,
        first_byte_seconds=None,
        timeout_reason="first_byte_timeout",
        cancelled=True,
    )


def _success() -> ProviderCommandResult:
    return ProviderCommandResult(
        returncode=0,
        stdout=json.dumps(
            {
                "markdown": "# retained evidence",
                "metadata": {"statusCode": 200, "contentType": "text/markdown"},
            }
        ).encode(),
        stderr="",
        elapsed_seconds=0.05,
        first_byte_seconds=0.01,
    )


class _Runner:
    def __init__(self, results: list[ProviderCommandResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def run(self, cmd, **kwargs):
        del cmd, kwargs
        self.calls += 1
        return self.results.pop(0)


def _adapter(runner: _Runner, retries: int) -> BoundedFirecrawlSearchAdapter:
    return BoundedFirecrawlSearchAdapter(
        runner=cast(BoundedSubprocessRunner, runner),
        deadline_policy=ExtractionDeadlinePolicy(
            first_byte_timeout_seconds=0.25,
            provider_operation_timeout_seconds=1.0,
            overall_candidate_timeout_seconds=2.0,
            transient_retries=2,
        ),
        first_byte_retry_policy=FirstByteTimeoutRetryPolicy(retries=retries),
    )


def test_first_byte_timeout_retries_once_then_returns_one_successful_candidate() -> (
    None
):
    runner = _Runner([_timeout(), _success()])
    result = _adapter(runner, 1).scrape_url("https://example.test/retry")
    assert runner.calls == 2
    assert result.transport_metadata["attempts"] == 2
    assert result.transport_metadata["preflight"]["classification"] == "suitable"
    attempts = result.transport_metadata["provider_sub_attempts"]
    assert [item["reason_code"] for item in attempts] == [
        "first_byte_timeout",
        "suitable",
    ]


def test_first_byte_timeout_exhaustion_preserves_timeout_reason_and_count() -> None:
    runner = _Runner([_timeout(), _timeout()])
    result = _adapter(runner, 1).scrape_url("https://example.test/exhaust")
    assert runner.calls == 2
    preflight = result.transport_metadata["preflight"]
    assert preflight["classification"] == "timeout"
    assert preflight["reason_code"] == "first_byte_timeout"
    assert "bounded_attempts=2" in preflight["reason"]


def test_first_byte_retry_can_be_disabled() -> None:
    runner = _Runner([_timeout()])
    result = _adapter(runner, 0).scrape_url("https://example.test/disabled")
    assert runner.calls == 1
    assert result.transport_metadata["preflight"]["reason_code"] == "first_byte_timeout"


def test_provider_operation_timeout_is_not_first_byte_retried() -> None:
    operation_timeout = ProviderCommandResult(
        returncode=-15,
        stdout=b"x",
        stderr="ETIMEDOUT: provider operation deadline exceeded",
        elapsed_seconds=0.75,
        first_byte_seconds=0.01,
        timeout_reason="provider_operation_timeout",
        cancelled=True,
    )
    runner = _Runner([operation_timeout])
    result = _adapter(runner, 2).scrape_url("https://example.test/operation")
    assert runner.calls == 1
    assert (
        result.transport_metadata["preflight"]["reason_code"]
        == "provider_operation_timeout"
    )


def test_non_transient_content_rejection_is_not_retried() -> None:
    rejected = ProviderCommandResult(
        returncode=0,
        stdout=json.dumps(
            {
                "markdown": "",
                "metadata": {"statusCode": 200, "contentType": "text/html"},
            }
        ).encode(),
        stderr="",
        elapsed_seconds=0.05,
        first_byte_seconds=0.01,
    )
    runner = _Runner([rejected])
    result = _adapter(runner, 3).scrape_url("https://example.test/empty")
    assert runner.calls == 1
    assert result.transport_metadata["preflight"]["classification"] == "empty_content"


def test_first_byte_retry_policy_env_is_stable_and_validated(monkeypatch) -> None:
    monkeypatch.setenv(FIRST_BYTE_TIMEOUT_RETRIES_ENV, "3")
    assert FirstByteTimeoutRetryPolicy.from_env().retries == 3


def test_real_runner_reaps_timed_out_child_before_retry_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter_path = tmp_path / "attempt-count"
    pid_path = tmp_path / "first-attempt.pid"
    executable = tmp_path / "firecrawl"
    executable.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "from pathlib import Path",
                "import time",
                f"counter = Path({str(counter_path)!r})",
                f"pid_file = Path({str(pid_path)!r})",
                "if not counter.exists():",
                "    counter.write_text('1', encoding='utf-8')",
                "    pid_file.write_text(str(os.getpid()), encoding='utf-8')",
                "    time.sleep(10)",
                "else:",
                "    print(json.dumps({",
                "        'markdown': '# retained evidence',",
                "        'metadata': {",
                "            'statusCode': 200,",
                "            'contentType': 'text/markdown',",
                "        },",
                "    }), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    adapter = BoundedFirecrawlSearchAdapter(
        runner=BoundedSubprocessRunner(),
        deadline_policy=ExtractionDeadlinePolicy(
            first_byte_timeout_seconds=0.20,
            provider_operation_timeout_seconds=1.0,
            overall_candidate_timeout_seconds=2.0,
            transient_retries=0,
        ),
        first_byte_retry_policy=FirstByteTimeoutRetryPolicy(retries=1),
    )
    started = time.monotonic()
    result = adapter.scrape_url("https://example.test/real-runner")
    elapsed = time.monotonic() - started

    assert result.transport_metadata["attempts"] == 2
    assert result.transport_metadata["preflight"]["classification"] == "suitable"
    attempts = result.transport_metadata["provider_sub_attempts"]
    assert [item["reason_code"] for item in attempts] == [
        "first_byte_timeout",
        "suitable",
    ]
    assert elapsed < 2.0
    assert pid_path.exists()
    first_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)
