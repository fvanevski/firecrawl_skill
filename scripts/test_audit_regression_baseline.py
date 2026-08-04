"""Strict expected-failure baseline for audited release-candidate defects."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import drain_index_jobs as drain_module
import pytest
from research_store import cli as research_store_cli
from research_store.acquisition_authority import ACQUISITION_ENTRY_STATES
from research_store.blob import ContentAddressedBlobStore
from research_store.parsing_legacy import parse_raw_search_response
from research_store.postgres import PostgresUnitOfWork

AUDITED_COMPLETE = 1_344
AUDITED_RUNNING_LIVE = 32
AUDITED_TOTAL = 1_376


def _worker_result(*, complete: int, running_live: int) -> dict[str, int]:
    return {
        "claimed": 0,
        "complete": complete,
        "complete_manifests": complete,
        "expected": AUDITED_TOTAL,
        "claimable": 0,
        "running_live": running_live,
        "failed": 0,
        "lease_lost": 0,
    }


def _completed_process(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["research-db", "worker", "--once"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


class _RowsCursor:
    def __init__(self, rows: list[tuple[Any, str]]) -> None:
        self._rows = rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: str, _params: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, str]]:
        return self._rows


class _RowsConnection:
    def __init__(self, rows: list[tuple[Any, str]]) -> None:
        self._rows = rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _RowsCursor:
        return _RowsCursor(self._rows)


class _BatchTimingCursor:
    def __init__(self, connection: _BatchTimingConnection) -> None:
        self.connection = connection
        self._row: tuple[datetime] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.lower().split())
        self.connection.statements.append(normalized)
        params = params or ()

        if normalized.startswith("select") and (
            "max(" in normalized or "greatest(" in normalized
        ):
            self._row = (self.connection.latest_terminal_at,)

        if "update ingestion_batches" not in normalized:
            return
        if "completed_at=now()" in normalized:
            self.connection.completed_at = self.connection.database_now
        elif "max(" in normalized or "greatest(" in normalized:
            self.connection.completed_at = self.connection.latest_terminal_at
        else:
            timestamps = [value for value in params if isinstance(value, datetime)]
            if timestamps:
                self.connection.completed_at = max(timestamps)

    def fetchone(self) -> tuple[datetime] | None:
        return self._row


class _BatchTimingConnection:
    def __init__(self, database_now: datetime, latest_terminal_at: datetime) -> None:
        self.database_now = database_now
        self.latest_terminal_at = latest_terminal_at
        self.completed_at: datetime | None = None
        self.statements: list[str] = []

    def cursor(self) -> _BatchTimingCursor:
        return _BatchTimingCursor(self)


def _blob_health(
    monkeypatch: pytest.MonkeyPatch,
    blob_root: Path,
    rows: list[tuple[Any, str]],
) -> dict[str, Any]:
    monkeypatch.setattr(
        research_store_cli,
        "_db",
        lambda _config: _RowsConnection(rows),
    )
    return research_store_cli._blob_health(SimpleNamespace(blob_root=blob_root))


def test_audited_fixture_conserves_exact_membership() -> None:
    assert AUDITED_COMPLETE + AUDITED_RUNNING_LIVE == AUDITED_TOTAL


def test_valid_nonempty_provider_response_remains_successful() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps(
            {
                "success": True,
                "data": [
                    {
                        "url": "https://example.test/result",
                        "title": "Audited control result",
                    }
                ],
            }
        )
    )

    assert status == "succeeded"
    assert count == 1
    assert summary["result_count"] == 1
    assert error is None


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-01 tracked by #208: running-live jobs must prevent quiescent success",
)
def test_rc_01_running_live_prevents_quiescent_success() -> None:
    result = drain_module.drain_index_jobs(
        Path("research-db"),
        max_batches=1,
        runner=lambda _command: _completed_process(
            _worker_result(
                complete=AUDITED_COMPLETE,
                running_live=AUDITED_RUNNING_LIVE,
            )
        ),
    )

    assert result != 0, (
        "claimed=0 was treated as quiescent while 32 completion-critical jobs "
        "still had live leases"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-02 tracked by #209: drain must reobserve the final 32 completions",
)
def test_rc_02_drain_reobserves_final_32_completions() -> None:
    states = [
        _worker_result(
            complete=AUDITED_COMPLETE,
            running_live=AUDITED_RUNNING_LIVE,
        ),
        _worker_result(complete=AUDITED_TOTAL, running_live=0),
    ]
    observations: list[dict[str, int]] = []

    def runner(_command: object) -> subprocess.CompletedProcess[str]:
        state = states[len(observations)]
        observations.append(state)
        return _completed_process(state)

    result = drain_module.drain_index_jobs(
        Path("research-db"),
        max_batches=2,
        runner=runner,
    )

    assert result == 0
    assert observations == states, (
        "the drain did not perform the immediately following observation after "
        "the first claimed=0 state"
    )
    assert observations[1]["complete_manifests"] - observations[0][
        "complete_manifests"
    ] == AUDITED_RUNNING_LIVE


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-04 tracked by #212: direct acquisition must reject created runs",
)
def test_rc_04_created_state_is_not_acquisition_eligible() -> None:
    assert "created" not in ACQUISITION_ENTRY_STATES


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-08 tracked by #213: provider-declared no-results must be empty",
)
def test_rc_08_provider_declared_no_results_are_empty() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps(
            {
                "success": False,
                "error": "No results found",
                "data": [],
            }
        )
    )

    assert (status, count, error) == ("empty", 0, None), summary


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-09 tracked by #213: stage markers are not provider responses",
)
def test_rc_09_stage_marker_is_not_a_valid_search_response() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps({"stage": "planning"})
    )

    assert status == "parse_error", summary
    assert count == 0
    assert error is not None


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "RC-11 tracked by #217: batch completion follows latest constituent "
        "terminal time"
    ),
)
def test_rc_11_batch_completion_uses_latest_constituent_terminal_time() -> None:
    database_now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    latest_terminal_at = datetime(2026, 8, 4, 12, 10, tzinfo=timezone.utc)
    connection = _BatchTimingConnection(database_now, latest_terminal_at)
    unit_of_work = object.__new__(PostgresUnitOfWork)
    unit_of_work.connection = connection

    unit_of_work.finish_ingestion_batch(UUID(int=1), "complete")

    assert connection.completed_at == latest_terminal_at, (
        "ingestion_batches.completed_at was bound to the finishing statement's "
        "wall clock instead of the latest constituent terminal timestamp"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-16 tracked by #219: zero eligible blobs must be inconclusive",
)
def test_rc_16_zero_blob_verification_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    health = _blob_health(monkeypatch, tmp_path, [])

    assert health.get("ok") is not True, (
        "zero referenced and zero examined blobs were presented as a positive "
        "integrity assertion"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RC-17 tracked by #220: orphan inventory must not fail referenced integrity",
)
def test_rc_17_orphans_do_not_fail_referenced_blob_integrity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    referenced = store.put(BytesIO(b"referenced audited payload"))
    orphan = store.put(BytesIO(b"unrelated orphan payload"))

    health = _blob_health(
        monkeypatch,
        tmp_path,
        [(UUID(int=2), referenced.sha256)],
    )

    assert orphan.sha256 in health["unreferenced"]
    assert health["missing_or_corrupt"] == []
    assert health["ok"] is True, (
        "an unrelated orphan blob invalidated otherwise healthy referenced-blob "
        "integrity"
    )
