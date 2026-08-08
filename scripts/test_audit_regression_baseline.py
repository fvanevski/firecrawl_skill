"""Strict expected-failure baseline for audited release-candidate defects."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import drain_index_jobs as drain_module
import pytest
from research_store import cli as research_store_cli
from research_store.acquisition_authority import (
    AcquisitionPreflightError,
    require_authoritative_acquisition,
)
from research_store.blob import ContentAddressedBlobStore
from research_store.indexing import IndexWorker
from research_store.orchestrator import ResearchOrchestrator
from research_store.parsing_legacy import parse_raw_search_response
from research_store.postgres import PostgresUnitOfWork
from research_store.stages import ContextKeys, StageResult
from research_store.workflow_service import (
    RunIndexProgress,
    WorkflowBoundaryError,
    WorkflowOperationService,
)

AUDITED_COMPLETE = 1_344
AUDITED_RUNNING_LIVE = 32
AUDITED_TOTAL = 1_376
AUDITED_FINGERPRINT = "audit-index-fingerprint"
_TERMINAL_EXTRACTION_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})


def _worker_result(*, complete: int, running_live: int) -> dict[str, int]:
    return {
        "claimed": 0,
        "complete": complete,
        "complete_manifests": complete,
        "expected": AUDITED_TOTAL,
        "claimable": 0,
        "running_live": running_live,
        "running_expired": 0,
        "retryable_failed": 0,
        "dead": 0,
        "missing_job": 0,
        "wrong_fingerprint": 0,
        "manifest_inconsistent": 0,
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


def _assert_rejected(
    error_type: type[BaseException],
    action: Callable[[], object],
    *,
    contains: str,
) -> BaseException:
    try:
        action()
    except error_type as exc:
        assert contains in str(exc)
        return exc
    except Exception as exc:
        raise AssertionError(
            f"expected {error_type.__name__}, got {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"expected {error_type.__name__} containing {contains!r}")


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


class _IndexCensusRepository:
    def __init__(self, sealed_entity_ids: list[UUID]) -> None:
        self.sealed_entity_ids = sealed_entity_ids
        self.claim_options: dict[str, Any] | None = None
        self.census_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def claim_jobs(self, limit: int, **options: Any) -> list[dict[str, Any]]:
        assert limit == 64
        assert options["entity_ids"] == self.sealed_entity_ids
        assert options["fingerprint"] == AUDITED_FINGERPRINT
        self.claim_options = options
        return []

    def heartbeat_worker(self, _worker_id: str, _metadata: dict[str, Any]) -> None:
        return None

    def _census(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        self.census_calls.append((args, kwargs))
        return {
            "expected": AUDITED_TOTAL,
            "complete": AUDITED_COMPLETE,
            "claimable": 0,
            "running_live": AUDITED_RUNNING_LIVE,
            "running_expired": 0,
            "retryable_failed": 0,
            "dead": 0,
            "missing_job": 0,
            "wrong_fingerprint": 0,
            "manifest_inconsistent": 0,
        }

    def __getattr__(self, name: str) -> Any:
        if "census" in name:
            return self._census
        raise AttributeError(name)


class _IndexCensusUnitOfWork:
    def __init__(self, repository: _IndexCensusRepository) -> None:
        self.index_jobs = repository

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _PreflightCursor:
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: object = None) -> None:
        normalized = " ".join(query.lower().split())
        if "select version_num from alembic_version" in normalized:
            self._rows = [("audit-head",)]
        elif "show transaction_read_only" in normalized:
            self._rows = [("off",)]
        elif "has_table_privilege" in normalized:
            self._rows = [(True,)]
        elif "from research_runs" in normalized and "for share" in normalized:
            self._rows = [(self.run_id, "created", 0)]
        else:
            raise AssertionError(f"unexpected acquisition preflight SQL: {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _PreflightConnection:
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        self.rolled_back = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _PreflightCursor:
        return _PreflightCursor(self.run_id)

    def rollback(self) -> None:
        self.rolled_back = True


class _LifecycleRunService:
    def __init__(self, run_id: UUID, external_id: str) -> None:
        self.run_id = run_id
        self.external_id = external_id
        self.state = "created"
        self.lifecycle_revision = 0
        self.transitions: list[str] = []
        self.uow_factory = lambda: None

    def status(
        self,
        *,
        run_id: UUID | None = None,
        external_id: str | None = None,
    ) -> SimpleNamespace:
        assert run_id in (None, self.run_id)
        assert external_id in (None, self.external_id)
        return SimpleNamespace(
            id=self.run_id,
            external_id=self.external_id,
            state=self.state,
            lifecycle_revision=self.lifecycle_revision,
        )

    def transition(self, _run_id: UUID, next_state: str, **_kwargs: Any) -> None:
        self.transitions.append(next_state)
        self.state = next_state
        self.lifecycle_revision += 1


class _LifecycleInvocationService:
    def __init__(self) -> None:
        self.begin_calls: list[tuple[Any, ...]] = []

    def begin(self, *args: Any, **_kwargs: Any) -> SimpleNamespace:
        self.begin_calls.append(args)
        return SimpleNamespace(status="running")


class _ProviderResponseRecordingRunService:
    def __init__(self) -> None:
        self.provider_response_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.lifecycle_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record_search_response(self, *args: Any, **kwargs: Any) -> None:
        self.provider_response_calls.append((args, kwargs))

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("record_", "append_")):

            def recorder(*args: Any, **kwargs: Any) -> None:
                self.lifecycle_calls.append((name, args, kwargs))

            return recorder
        raise AttributeError(name)


class _SuccessfulStage:
    def execute(self, *_args: Any, **_kwargs: Any) -> StageResult:
        return StageResult.ok("planning", "deterministic stage completed")


class _BatchTimingCursor:
    def __init__(self, connection: _BatchTimingConnection) -> None:
        self.connection = connection
        self._row: tuple[datetime] | None = None
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @staticmethod
    def _parameter_values(params: tuple[Any, ...]) -> list[Any]:
        values: list[Any] = []
        for value in params:
            if isinstance(value, (list, tuple, set, frozenset)):
                values.extend(value)
            else:
                values.append(value)
        return values

    def _validate_constituent_aggregation(
        self,
        normalized: str,
        params: tuple[Any, ...],
    ) -> None:
        # Direct association path skips asset_snapshots; legacy path requires it.
        has_direct_association = "extraction_attempt_id" in normalized
        required_relations = [
            "ingestion_batch_assets",
            "extraction_attempts",
        ]
        if not has_direct_association:
            required_relations.append("asset_snapshots")
        for token in required_relations:
            assert token in normalized, f"missing exact constituent token: {token}"
        # started_at derivation uses start_time; completed_at uses end_time.
        assert any(col in normalized for col in ("start_time", "end_time")), (
            "constituent timing query must reference a timestamp column"
        )
        # exit_status filtering is required only for terminal-time queries,
        # not for the started_at derivation which scans all attempts.
        if "min(ea.start_time)" not in normalized:
            assert "exit_status" in normalized, "must filter on exit_status"
        assert "now()" not in normalized, "batch completion must not use statement time"
        assert self.connection.target_batch_id in params

        # Terminal-state filtering is only required for completed_at queries;
        # started_at derivation scans all attempts without exit_status filter.
        if "min(ea.start_time)" not in normalized:
            parameter_values = self._parameter_values(params)
            states_in_sql = {
                state for state in _TERMINAL_EXTRACTION_STATES if state in normalized
            }
            states_in_params = {
                value
                for value in parameter_values
                if isinstance(value, str) and value in _TERMINAL_EXTRACTION_STATES
            }
            assert states_in_sql | states_in_params == _TERMINAL_EXTRACTION_STATES, (
                "terminal extraction-state filtering is incomplete"
            )

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.lower().split())
        params = params or ()
        self.connection.statements.append((normalized, params))

        is_batch_update = "update ingestion_batches" in normalized
        # Accept both the legacy snapshot-join path and the direct
        # extraction_attempt_id association path.
        has_legacy_relations = all(
            relation in normalized
            for relation in (
                "ingestion_batch_assets",
                "asset_snapshots",
                "extraction_attempts",
            )
        )
        has_direct_association = (
            "ingestion_batch_assets" in normalized
            and "extraction_attempts" in normalized
            and "extraction_attempt_id" in normalized
        )
        has_constituent_relations = has_legacy_relations or has_direct_association

        if has_constituent_relations and not is_batch_update:
            # Only validate constituent aggregation for SELECT queries.
            # UPDATE statements carry CTEs that reference the relations but
            # do not directly contain the timing columns.
            self._validate_constituent_aggregation(normalized, params)
            # started_at derivation (MIN start_time) is separate from
            # completed_at derivation (MAX end_time); only the latter drives
            # the batch update timestamp.
            is_started_at_query = "min(ea.start_time)" in normalized
            if not is_started_at_query:
                terminal_at = self.connection.latest_terminal_for_target_batch()
                self.connection.selected_terminal_at = terminal_at
                self._row = (terminal_at,)
                return
            # started_at query returns the earliest constituent start; skip
            # None values that represent attempts without terminal outcomes.
            valid_starts = [
                a["end_time"]
                for a in self.connection.extraction_attempts.values()
                if a.get("end_time") is not None
            ]
            earliest = min(valid_starts) if valid_starts else None
            self._row = (earliest,)
            return

        if is_batch_update:
            assert self.connection.target_batch_id in params
            if "completed_at" not in normalized:
                self.rowcount = 1
                return
            assert "now()" not in normalized, (
                "ingestion_batches.completed_at still uses statement wall clock"
            )
            assert self.connection.selected_terminal_at is not None, (
                "batch update did not derive a timestamp from exact constituents"
            )
            timestamps = [value for value in params if isinstance(value, datetime)]
            assert len(timestamps) >= 1, (
                "batch update must include at least one constituent-derived timestamp"
            )
            assert timestamps[0] == self.connection.selected_terminal_at, (
                "first timestamp must be the constituent-derived completed_at"
            )
            self.connection.batches[self.connection.target_batch_id]["completed_at"] = (
                timestamps[0]
            )
            self.rowcount = 1
            return

        if (
            "from ingestion_batches" in normalized
            and "for update" in normalized
            and self.connection.target_batch_id in params
        ):
            self._row = (self.connection.target_batch_id,)
            return

        if "information_schema.columns" in normalized and "sealed_at" in normalized:
            # Backward-compatibility column-existence probe.
            self._row = (1,)
            return

        if (
            "information_schema.columns" in normalized
            and "extraction_attempt_id" in normalized
            and "ingestion_batch_assets" in normalized
        ):
            # Column-existence probe for extraction_attempt_id; present on v43+.
            self._row = (1,)
            return

        if (
            "from ingestion_batches" in normalized
            and "started_at" in normalized
            and "for update" not in normalized
        ):
            # started_at fallback read; return the existing row value.
            batch_data = self.connection.batches.get(
                self.connection.target_batch_id, {}
            )
            self._row = (batch_data.get("started_at"),)
            return

        raise AssertionError(f"unexpected batch completion SQL: {normalized}")

    def fetchone(self) -> tuple[datetime] | None:
        return self._row


class _BatchTimingConnection:
    def __init__(self) -> None:
        self.target_batch_id = UUID(int=11)
        self.unrelated_batch_id = UUID(int=12)
        self.expected_terminal_at = datetime(2026, 8, 4, 12, 10, tzinfo=timezone.utc)
        self.batches: dict[UUID, dict[str, Any]] = {
            self.target_batch_id: {"status": "running", "completed_at": None},
            self.unrelated_batch_id: {"status": "running", "completed_at": None},
        }
        self.batch_assets = [
            {
                "batch_id": self.target_batch_id,
                "snapshot_id": UUID(int=101),
                "status": "complete",
            },
            {
                "batch_id": self.target_batch_id,
                "snapshot_id": UUID(int=102),
                "status": "failed",
            },
            {
                "batch_id": self.target_batch_id,
                "snapshot_id": UUID(int=103),
                "status": "complete",
            },
            {
                "batch_id": self.unrelated_batch_id,
                "snapshot_id": UUID(int=201),
                "status": "complete",
            },
        ]
        self.snapshots = {
            UUID(int=101): {"extraction_attempt_id": UUID(int=1_001)},
            UUID(int=102): {"extraction_attempt_id": UUID(int=1_002)},
            UUID(int=103): {"extraction_attempt_id": UUID(int=1_003)},
            UUID(int=201): {"extraction_attempt_id": UUID(int=2_001)},
        }
        self.extraction_attempts = {
            UUID(int=1_001): {
                "exit_status": "succeeded",
                "end_time": datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
            },
            UUID(int=1_002): {
                "exit_status": "failed",
                "end_time": self.expected_terminal_at,
            },
            UUID(int=1_003): {
                "exit_status": "succeeded",
                "end_time": None,
            },
            UUID(int=2_001): {
                "exit_status": "succeeded",
                "end_time": datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc),
            },
        }
        self.selected_terminal_at: datetime | None = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def latest_terminal_for_target_batch(self) -> datetime:
        terminal_times: list[datetime] = []
        for asset in self.batch_assets:
            if asset["batch_id"] != self.target_batch_id:
                continue
            snapshot = self.snapshots[asset["snapshot_id"]]
            attempt = self.extraction_attempts[snapshot["extraction_attempt_id"]]
            if (
                attempt["exit_status"] in _TERMINAL_EXTRACTION_STATES
                and attempt["end_time"] is not None
            ):
                terminal_times.append(attempt["end_time"])
        assert terminal_times
        return max(terminal_times)

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


@pytest.mark.parametrize(
    ("raw_payload", "http_status", "expected_status"),
    [
        (b"{not-json", 200, "parse_error"),
        (json.dumps({"success": False, "error": "rate limit"}), 200, "provider_error"),
        (b"\xff\xfe", 503, "provider_error"),
    ],
)
def test_search_response_failures_remain_distinct(
    raw_payload: bytes | str,
    http_status: int,
    expected_status: str,
) -> None:
    status, count, _summary, error = parse_raw_search_response(
        raw_payload,
        http_status=http_status,
    )

    assert status == expected_status
    assert count == 0
    assert error is not None


def test_rc_01_exact_index_job_census_preserves_sealed_membership() -> None:
    sealed_entity_ids = [UUID(int=index + 1) for index in range(AUDITED_TOTAL)]
    repository = _IndexCensusRepository(sealed_entity_ids)
    worker = IndexWorker(
        uow_factory=lambda: _IndexCensusUnitOfWork(repository),
        index=SimpleNamespace(),
        embedder=SimpleNamespace(fingerprint=AUDITED_FINGERPRINT),
        worker_id="audit-regression",
    )

    result = worker.run_batch(limit=64, entity_ids=sealed_entity_ids)
    raw_census = result.get("census")
    census_source = raw_census if isinstance(raw_census, dict) else result
    census = {
        "expected": census_source.get("expected"),
        "complete": census_source.get(
            "complete_manifests", census_source.get("complete")
        ),
        "claimable": census_source.get("claimable"),
        "running_live": census_source.get("running_live"),
        "running_expired": census_source.get("running_expired"),
        "retryable_failed": census_source.get("retryable_failed"),
        "dead": census_source.get("dead"),
        "missing_job": census_source.get("missing_job"),
        "wrong_fingerprint": census_source.get("wrong_fingerprint"),
        "manifest_inconsistent": census_source.get("manifest_inconsistent"),
    }

    classes = (
        "complete",
        "claimable",
        "running_live",
        "running_expired",
        "retryable_failed",
        "dead",
        "missing_job",
        "wrong_fingerprint",
        "manifest_inconsistent",
    )
    assert census["expected"] == AUDITED_TOTAL
    assert census["complete"] == AUDITED_COMPLETE
    assert census["claimable"] == 0
    assert census["running_live"] == AUDITED_RUNNING_LIVE
    assert all(isinstance(census[name], int) and census[name] >= 0 for name in classes)
    assert sum(census[name] for name in classes) == census["expected"]
    assert repository.claim_options is not None


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
        waiter=lambda _seconds: False,
    )

    assert result == 0
    assert observations == states, (
        "the drain did not perform the immediately following observation after "
        "the first claimed=0 state"
    )
    assert (
        observations[1]["complete_manifests"] - observations[0]["complete_manifests"]
        == AUDITED_RUNNING_LIVE
    )


def test_rc_04_direct_acquisition_obeys_lifecycle_boundaries(
    tmp_path: Path,
) -> None:
    run_id = UUID(int=4)
    external_run_id = "fr_audit_rc_04"
    connection = _PreflightConnection(run_id)
    config = SimpleNamespace(
        database_url="postgresql://audit.invalid/firecrawl_test",
        blob_root=tmp_path / "blobs",
        require_database=lambda: None,
    )

    _assert_rejected(
        AcquisitionPreflightError,
        lambda: require_authoritative_acquisition(
            run_id=run_id,
            config=config,
            connect_factory=lambda _database_url: connection,
            expected_heads_factory=lambda: frozenset({"audit-head"}),
        ),
        contains="created",
    )

    direct_run_service = _LifecycleRunService(run_id, external_run_id)
    direct_invocations = _LifecycleInvocationService()
    direct_service = WorkflowOperationService(
        direct_run_service,
        direct_invocations,
    )
    _assert_rejected(
        WorkflowBoundaryError,
        lambda: direct_service.begin_operation(
            external_run_id,
            "fi_audit_rc_04",
            "fsearch",
            {"query": "audited lifecycle boundary"},
        ),
        contains="created",
    )
    assert direct_run_service.transitions == []
    assert direct_run_service.lifecycle_revision == 0
    assert direct_invocations.begin_calls == []

    finish_run_service = _LifecycleRunService(run_id, external_run_id)
    finish_service = WorkflowOperationService(
        finish_run_service,
        _LifecycleInvocationService(),
    )
    finish_service.index_progress = lambda _run_id: RunIndexProgress(
        assets=0,
        chunks=0,
        pending=0,
        running=0,
        failed=0,
        dead=0,
        complete=0,
    )
    _assert_rejected(
        WorkflowBoundaryError,
        lambda: finish_service.finish_run(
            external_run_id,
            outcome="satisfied",
        ),
        contains="cannot finish from created",
    )
    assert finish_run_service.transitions == []
    assert finish_run_service.lifecycle_revision == 0


@pytest.mark.parametrize(
    "raw_payload",
    [
        pytest.param(b"No results found.\n", id="audited-plaintext"),
        pytest.param(
            json.dumps(
                {
                    "success": False,
                    "error": "No results found",
                    "data": [],
                }
            ),
            id="generic-json-envelope",
        ),
        pytest.param(
            json.dumps({"success": True, "data": []}),
            id="successful-empty-envelope",
        ),
    ],
)
def test_rc_08_provider_declared_no_results_are_empty(
    raw_payload: bytes | str,
) -> None:
    status, count, summary, error = parse_raw_search_response(raw_payload)

    assert (status, count, error) == ("empty", 0, None), summary


def test_rc_09_stage_execution_does_not_write_provider_response() -> None:
    run_service = _ProviderResponseRecordingRunService()
    orchestrator = object.__new__(ResearchOrchestrator)
    orchestrator.run_service = run_service
    orchestrator._stages = {"planning": _SuccessfulStage()}

    result = orchestrator._execute_stage(
        "planning",
        UUID(int=9),
        0,
        None,
        "created",
        {ContextKeys.WAVE_COUNT: 0},
    )

    assert result.error is None
    assert run_service.provider_response_calls == [], (
        "ResearchOrchestrator._execute_stage() persisted lifecycle telemetry through "
        "the provider search-response writer"
    )


def test_rc_11_batch_completion_uses_latest_constituent_terminal_time() -> None:
    connection = _BatchTimingConnection()
    unit_of_work = object.__new__(PostgresUnitOfWork)
    unit_of_work.connection = connection

    unit_of_work.finish_ingestion_batch(connection.target_batch_id, "complete")

    assert connection.batches[connection.target_batch_id]["completed_at"] == (
        connection.expected_terminal_at
    )
    assert connection.batches[connection.unrelated_batch_id]["completed_at"] is None
    assert connection.selected_terminal_at == connection.expected_terminal_at


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
