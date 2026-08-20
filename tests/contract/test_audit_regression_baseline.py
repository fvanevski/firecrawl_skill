"""Strict expected-failure baseline for audited release-candidate defects."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID

import drain_index_jobs as drain_module
import pytest

from firecrawl_skill.research_store import cli as research_store_cli
from firecrawl_skill.research_store.acquisition_authority import (
    AcquisitionPreflightError,
    require_authoritative_acquisition,
)
from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.indexing import IndexWorker
from firecrawl_skill.research_store.invocation_service import InvocationService
from firecrawl_skill.research_store.parsing_legacy import parse_raw_search_response
from firecrawl_skill.research_store.postgres import PostgresUnitOfWork
from firecrawl_skill.research_store.run_service import ResearchRunService
from firecrawl_skill.research_store.stages import ContextKeys, StageResult
from firecrawl_skill.research_store.workflow_service import (
    RunIndexProgress,
    WorkflowBoundaryError,
    WorkflowOperationService,
)

AUDITED_COMPLETE = 1_344
AUDITED_RUNNING_LIVE = 32
AUDITED_TOTAL = 1_376
AUDITED_FINGERPRINT = "audit-index-fingerprint"


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
    """Small exact-member fake for the RC-11 production repository method."""

    def __init__(self, connection: _BatchTimingConnection) -> None:
        self.connection = connection
        self._row: tuple[Any, ...] | None = None
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.lower().split())
        params = params or ()
        self.connection.statements.append((normalized, params))
        self._row = None
        self._rows = []
        self.rowcount = 0

        if "information_schema.columns" in normalized and "sealed_at" in normalized:
            self._row = (1,)
            return
        if (
            "information_schema.columns" in normalized
            and "constituent_started_at" in normalized
            and "constituent_completed_at" in normalized
        ):
            self._row = (2,)
            return
        if (
            normalized.startswith("select 1 from ingestion_batches")
            and "for update" in normalized
        ):
            assert params == (self.connection.target_batch_id,)
            self._row = (1,)
            return
        if (
            "from ingestion_batch_assets iba" in normalized
            and "left join extraction_attempts ea" in normalized
        ):
            assert params == (self.connection.target_batch_id,)
            assert "ea.start_time" in normalized
            assert "ea.end_time" in normalized
            assert "iba.extraction_attempt_id" in normalized
            assert "now()" not in normalized
            self._rows = self.connection.member_rows()
            return
        if normalized.startswith("update ingestion_batches"):
            assert params[-1] == self.connection.target_batch_id
            assert "started_at=%s" in normalized
            assert "completed_at=%s" in normalized
            assert "sealed_at=%s" in normalized
            assert "outcome_summary=%s::jsonb" in normalized
            status, _error, started_at, completed_at, _sealed_at, summary, _batch = (
                params
            )
            self.connection.batches[self.connection.target_batch_id].update(
                {
                    "status": status,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "outcome_summary": json.loads(summary),
                }
            )
            self.rowcount = 1
            return

        raise AssertionError(f"unexpected batch completion SQL: {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _BatchTimingConnection:
    def __init__(self) -> None:
        self.target_batch_id = UUID(int=11)
        self.unrelated_batch_id = UUID(int=12)
        self.expected_started_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        self.expected_terminal_at = datetime(2026, 8, 4, 12, 10, tzinfo=timezone.utc)
        self.batches: dict[UUID, dict[str, Any]] = {
            self.target_batch_id: {
                "status": "running",
                "started_at": None,
                "completed_at": None,
            },
            self.unrelated_batch_id: {
                "status": "running",
                "started_at": None,
                "completed_at": None,
            },
        }
        self.members = [
            (
                UUID(int=101),
                0,
                "complete",
                UUID(int=1_001),
                self.expected_started_at,
                datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
                "succeeded",
                "none",
            ),
            (
                UUID(int=102),
                1,
                "complete",
                UUID(int=1_002),
                datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
                self.expected_terminal_at,
                "succeeded",
                "none",
            ),
            (
                UUID(int=103),
                2,
                "complete",
                UUID(int=1_003),
                datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
                datetime(2026, 8, 4, 12, 7, tzinfo=timezone.utc),
                "succeeded",
                "none",
            ),
        ]
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def member_rows(self) -> list[tuple[Any, ...]]:
        return list(self.members)

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
    assert all(
        isinstance(census[name], int) and cast(int, census[name]) >= 0
        for name in classes
    )
    assert sum(cast(int, census[name]) for name in classes) == census["expected"]
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
            config=cast(StoreConfig | None, config),
            connect_factory=lambda _database_url: connection,
            expected_heads_factory=lambda: frozenset({"audit-head"}),
        ),
        contains="created",
    )

    direct_run_service = _LifecycleRunService(run_id, external_run_id)
    direct_invocations = _LifecycleInvocationService()
    direct_service = WorkflowOperationService(
        cast(ResearchRunService, direct_run_service),
        cast(InvocationService, direct_invocations),
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
        cast(ResearchRunService, finish_run_service),
        cast(InvocationService, _LifecycleInvocationService()),
    )
    finish_service.index_progress = cast(
        Any,
        lambda _run_id: RunIndexProgress(
            assets=0,
            chunks=0,
            pending=0,
            running=0,
            failed=0,
            dead=0,
            complete=0,
        ),
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
    from firecrawl_skill.research_store.checkpoint_orchestrator import (
        CheckpointResearchOrchestrator,
    )

    run_service = _ProviderResponseRecordingRunService()
    orchestrator = object.__new__(CheckpointResearchOrchestrator)
    orchestrator.run_service = cast(ResearchRunService, run_service)
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


def test_rc_11_batch_completion_uses_exact_constituent_start_and_terminal_times() -> (
    None
):
    connection = _BatchTimingConnection()
    unit_of_work = object.__new__(PostgresUnitOfWork)
    unit_of_work.connection = connection

    unit_of_work.finish_ingestion_batch(connection.target_batch_id, "complete")

    target = connection.batches[connection.target_batch_id]
    assert target["started_at"] == connection.expected_started_at
    assert target["completed_at"] == connection.expected_terminal_at
    assert target["outcome_summary"]["succeeded"] == 3
    assert target["outcome_summary"]["member_count"] == 3
    assert connection.batches[connection.unrelated_batch_id]["started_at"] is None
    assert connection.batches[connection.unrelated_batch_id]["completed_at"] is None


def test_rc_16_zero_blob_verification_is_inconclusive(tmp_path: Path) -> None:
    run_id = UUID(int=16)

    class Runs:
        def list_invocations(self, requested_run_id: UUID) -> list[dict[str, Any]]:
            assert requested_run_id == run_id
            return []

    class UnitOfWork:
        runs = Runs()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    report = ResearchRunService(
        lambda: UnitOfWork(),
        blob_store=ContentAddressedBlobStore(tmp_path),
    ).verify(run_id)

    assert report["status"] == "inconclusive"
    assert report["total"] == 0
    assert report["available"] == 0
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0


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

    assert orphan.sha256 in health["unreferenced_inventory"]
    assert health["missing_or_corrupt"] == []
    assert health["integrity"] == "pass", (
        "an unrelated orphan blob invalidated otherwise healthy referenced-blob "
        "integrity"
    )
    assert health["orphan_count"] == 1


def test_rc_17_connectivity_failure_classifies_network_policy_denial() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = PermissionError("Operation not permitted")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "network_policy_denial"


def test_rc_17_connectivity_failure_classifies_server_unavailable() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = ConnectionRefusedError("Connection refused")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "server_unavailable"


def test_rc_17_connectivity_failure_classifies_credential_failure() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = RuntimeError("authentication failed: invalid password")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "credential_failure"


def test_rc_17_connectivity_failure_classifies_network_namespace_denial() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = OSError("No route to host")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "network_namespace_denial"


def test_rc_17_connectivity_failure_classifies_database_rejection() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = RuntimeError("database connection failed: psycopg error")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "database_rejection"


def test_rc_17_connectivity_failure_classifies_query_runtime_failure() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    exc = RuntimeError("unexpected query runtime error")
    result = _classify_connectivity_failure(exc)
    assert result["status"] == "failure"
    assert result["reason_code"] == "query_runtime_failure"


def test_rc_17_orphan_inventory_is_separate_domain_from_integrity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Orphan blobs are reported in their own domain without affecting integrity."""
    store = ContentAddressedBlobStore(tmp_path)
    referenced = store.put(BytesIO(b"referenced payload"))
    orphan = store.put(BytesIO(b"unrelated orphan payload"))

    health = _blob_health(
        monkeypatch,
        tmp_path,
        [(UUID(int=2), referenced.sha256)],
    )

    assert health["integrity"] == "pass"
    assert health["missing_or_corrupt"] == []
    assert len(health["unreferenced_inventory"]) == 1
    assert health["orphan_count"] == 1
    assert orphan.sha256 in health["unreferenced_inventory"]


def test_rc_17_sandbox_denial_not_reported_as_database_failure() -> None:
    from firecrawl_skill.research_store.cli import _classify_connectivity_failure

    sandbox_exc = PermissionError("Operation not permitted")
    db_exc = RuntimeError("database connection failed: psycopg error")

    sandbox_result = _classify_connectivity_failure(sandbox_exc)
    db_result = _classify_connectivity_failure(db_exc)

    assert sandbox_result["reason_code"] == "network_policy_denial"
    assert db_result["reason_code"] == "database_rejection"
    assert sandbox_result["reason_code"] != db_result["reason_code"]
