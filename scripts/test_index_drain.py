"""Deterministic regression coverage for the census-aware index drain barrier."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent


def _load_module():
    path = SCRIPTS / "drain_index_jobs.py"
    spec = importlib.util.spec_from_file_location("drain_index_jobs_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _census(
    *,
    expected: int = 4,
    complete: int = 0,
    claimable: int = 0,
    running_live: int = 0,
    running_expired: int = 0,
    retryable_failed: int = 0,
    dead: int = 0,
    missing_job: int = 0,
    wrong_fingerprint: int = 0,
    manifest_inconsistent: int = 0,
) -> dict[str, int]:
    return {
        "expected": expected,
        "complete": complete,
        "complete_manifests": complete,
        "claimable": claimable,
        "running_live": running_live,
        "running_expired": running_expired,
        "retryable_failed": retryable_failed,
        "dead": dead,
        "missing_job": missing_job,
        "wrong_fingerprint": wrong_fingerprint,
        "manifest_inconsistent": manifest_inconsistent,
    }


def _worker(
    census: dict[str, int],
    *,
    claimed: int = 0,
    failed: int = 0,
    lease_lost: int = 0,
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    payload: dict[str, Any] = {
        "claimed": claimed,
        "failed": failed,
        "lease_lost": lease_lost,
        "census": census,
    }
    return subprocess.CompletedProcess(
        ["research-db", "worker", "--once"],
        returncode,
        json.dumps(payload),
        "",
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.waits: list[float] = []
        self.cancel = False

    def monotonic(self) -> float:
        return self.value

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.value += seconds
        return self.cancel


def _sequence_runner(
    responses: list[subprocess.CompletedProcess[str]],
) -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []

    def runner(argv: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return responses[len(calls) - 1]

    return runner, calls


def test_live_jobs_are_reobserved_after_zero_claim_census() -> None:
    module = _load_module()
    clock = _Clock()
    runner, calls = _sequence_runner(
        [
            _worker(_census(complete=3, running_live=1)),
            _worker(_census(complete=4)),
        ]
    )

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=2,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "complete"
    assert result.reason == "sealed_census_complete"
    assert result.exit_code == 0
    assert len(calls) == 2
    assert clock.waits == [0.25]


def test_expired_lease_is_reclaimed_by_a_following_scoped_batch() -> None:
    module = _load_module()
    clock = _Clock()
    runner, calls = _sequence_runner(
        [
            _worker(_census(complete=3, running_expired=1)),
            _worker(_census(complete=4), claimed=1),
        ]
    )

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=2,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "complete"
    assert len(calls) == 2
    assert clock.waits == [0.25]


def test_retryable_failure_is_retried_within_the_command_budget() -> None:
    module = _load_module()
    clock = _Clock()
    runner, calls = _sequence_runner(
        [
            _worker(
                _census(complete=3, retryable_failed=1),
                failed=1,
            ),
            _worker(_census(complete=4), claimed=1),
        ]
    )

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=2,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "complete"
    assert len(calls) == 2
    assert clock.waits == [0.25]


@pytest.mark.parametrize(
    "field",
    ["dead", "missing_job", "wrong_fingerprint", "manifest_inconsistent"],
)
def test_irrecoverable_census_classes_fail_closed(field: str) -> None:
    module = _load_module()
    clock = _Clock()
    values = {field: 1}
    runner, calls = _sequence_runner([_worker(_census(complete=3, **values))])

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "failed"
    assert result.recoverable is False
    assert result.reason == "irrecoverable_census_state"
    assert result.exit_code == 1
    assert len(calls) == 1
    assert clock.waits == []


def test_recoverable_deadline_returns_structured_resumable_result() -> None:
    module = _load_module()
    clock = _Clock()
    response = _worker(_census(complete=3, running_live=1))

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        return response

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=10,
        deadline_seconds=0.5,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=0.5,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    payload = result.to_dict()
    assert result.status == "resumable"
    assert result.exit_code == module.RESUMABLE_EXIT_CODE
    assert result.recoverable is True
    assert result.reason == "deadline_exhausted"
    assert payload["lifecycle_terminal"] is False
    assert payload["last_census"]["running_live"] == 1
    assert clock.waits == [0.25, 0.25]


def test_scoped_default_deadline_remains_bounded() -> None:
    module = _load_module()
    clock = _Clock()
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        clock.value += module.DEFAULT_SCOPED_DEADLINE_SECONDS + 1
        return _worker(_census(complete=3, running_live=1))

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=2,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "resumable"
    assert result.reason == "deadline_exhausted"
    assert result.batches == 1
    assert calls == 1


def test_auto_detected_census_also_uses_the_scoped_default_deadline() -> None:
    module = _load_module()
    clock = _Clock()
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        clock.value += module.DEFAULT_SCOPED_DEADLINE_SECONDS + 1
        return _worker(_census(complete=3, running_live=1))

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=2,
        runner=runner,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "resumable"
    assert result.reason == "deadline_exhausted"
    assert result.batches == 1
    assert calls == 1


def test_cancellation_interrupts_a_bounded_wait_without_terminalizing() -> None:
    module = _load_module()
    clock = _Clock()
    clock.cancel = True
    runner, calls = _sequence_runner([_worker(_census(complete=3, running_live=1))])

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "cancelled"
    assert result.exit_code == module.CANCELLED_EXIT_CODE
    assert result.recoverable is True
    assert result.to_dict()["lifecycle_terminal"] is False
    assert len(calls) == 1
    assert clock.waits == [0.25]


def test_cancellation_before_worker_skips_invocation() -> None:
    module = _load_module()
    clock = _Clock()
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        pytest.fail("worker must not run after cancellation")

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
        cancelled=lambda: True,
    )

    assert result.status == "cancelled"
    assert result.reason == "cancelled_before_worker"
    assert result.exit_code == module.CANCELLED_EXIT_CODE
    assert result.batches == 0
    assert calls == 0


def test_cancellation_after_worker_preempts_complete_census() -> None:
    module = _load_module()
    clock = _Clock()
    cancel_state = {"value": False}

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        cancel_state["value"] = True
        return _worker(_census(complete=4))

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
        cancelled=lambda: cancel_state["value"],
    )

    assert result.status == "cancelled"
    assert result.reason == "cancelled_after_worker"
    assert result.exit_code == module.CANCELLED_EXIT_CODE
    assert result.recoverable is True
    assert result.to_dict()["lifecycle_terminal"] is False
    assert result.batches == 1


def test_main_reports_structured_cancellation_during_scoped_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()

    def cancelled_setup(*_args: object, **_kwargs: object) -> object:
        raise module.DrainCancelled

    monkeypatch.setattr(module, "_run_scoped_runner", cancelled_setup)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: module.signal.SIG_DFL)

    exit_code = module.main(["--research-run-id", "fr_test"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == module.CANCELLED_EXIT_CODE
    assert payload["status"] == "cancelled"
    assert payload["reason"] == "cancelled_during_setup"
    assert payload["recoverable"] is True
    assert payload["lifecycle_terminal"] is False


def test_backoff_is_bounded_and_avoids_a_zero_claim_busy_loop() -> None:
    module = _load_module()
    clock = _Clock()
    response = _worker(_census(complete=3, running_live=1))
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return response

    result = module.drain_index_jobs_result(
        Path("research-db"),
        max_batches=4,
        deadline_seconds=100,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=0.5,
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "resumable"
    assert result.reason == "max_batches_exhausted"
    assert calls == 4
    assert clock.waits == [0.25, 0.5, 0.5]


def test_nonconserving_census_is_rejected() -> None:
    module = _load_module()
    clock = _Clock()
    invalid = _census(complete=3)
    runner, _calls = _sequence_runner([_worker(invalid)])

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "failed"
    assert result.reason.startswith("invalid_worker_result:")
    assert result.last_census is None


def test_required_census_cannot_fall_back_to_claimed_count() -> None:
    module = _load_module()
    clock = _Clock()

    def runner(argv: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv),
            0,
            '{"claimed": 0, "failed": 0, "lease_lost": 0}',
            "",
        )

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        require_census=True,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "failed"
    assert result.reason == "required_census_missing"


def test_unscoped_projection_maintenance_remains_backward_compatible() -> None:
    module = _load_module()
    clock = _Clock()
    responses = [
        subprocess.CompletedProcess(
            [], 0, '{"claimed": 1, "failed": 0, "lease_lost": 0}', ""
        ),
        subprocess.CompletedProcess(
            [], 0, '{"claimed": 0, "failed": 0, "lease_lost": 0}', ""
        ),
    ]
    runner, calls = _sequence_runner(responses)

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "complete"
    assert result.reason == "unscoped_queue_empty"
    assert len(calls) == 2
    assert clock.waits == []


def test_unscoped_default_does_not_add_an_elapsed_deadline() -> None:
    module = _load_module()
    clock = _Clock()
    responses = [
        subprocess.CompletedProcess(
            [], 0, '{"claimed": 1, "failed": 0, "lease_lost": 0}', ""
        ),
        subprocess.CompletedProcess(
            [], 0, '{"claimed": 0, "failed": 0, "lease_lost": 0}', ""
        ),
    ]
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        response = responses[calls]
        calls += 1
        clock.value += 301
        return response

    result = module.drain_index_jobs_result(
        Path("research-db"),
        runner=runner,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "complete"
    assert result.reason == "unscoped_queue_empty"
    assert calls == 2
    assert result.elapsed_seconds == 602


def test_unscoped_deadline_is_available_only_when_explicitly_requested() -> None:
    module = _load_module()
    clock = _Clock()
    calls = 0

    def runner(_argv: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        clock.value += 301
        return subprocess.CompletedProcess(
            [], 0, '{"claimed": 1, "failed": 0, "lease_lost": 0}', ""
        )

    result = module.drain_index_jobs_result(
        Path("research-db"),
        deadline_seconds=300,
        runner=runner,
        clock=clock.monotonic,
        waiter=clock.wait,
    )

    assert result.status == "failed"
    assert result.reason == "deadline_exhausted"
    assert result.batches == 1
    assert calls == 1


def test_run_scoped_runner_seals_postgresql_membership_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace
    from uuid import UUID

    module = _load_module()
    run_id = UUID(int=209)
    entity_ids = [UUID(int=1), UUID(int=2)]
    executions: list[tuple[str, tuple[Any, ...]]] = []
    run_batch_calls: list[tuple[int, list[UUID]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            executions.append((query, params))

        def fetchall(self) -> list[tuple[UUID]]:
            return [(entity_id,) for entity_id in entity_ids]

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    class UnitOfWork:
        parser_version = "parser-v1"
        normalization_version = "normalizer-v1"
        chunker_version = "chunker-v1"
        connection = Connection()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    corpus_service = SimpleNamespace(
        embedder=SimpleNamespace(fingerprint="fingerprint"),
        index=SimpleNamespace(),
        queue=SimpleNamespace(),
        uow_factory=lambda: UnitOfWork(),
    )
    config = SimpleNamespace(
        job_lease_seconds=30,
        max_index_attempts=5,
        require_database=lambda: None,
    )

    class StoreConfig:
        @classmethod
        def from_env(cls):
            return config

    class IndexWorker:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["uow_factory"] is corpus_service.uow_factory
            assert kwargs["lease_seconds"] == 30
            assert kwargs["max_attempts"] == 5

        def run_batch(self, limit: int, entity_ids: list[UUID]) -> dict[str, Any]:
            run_batch_calls.append((limit, entity_ids))
            return {
                "claimed": 0,
                "failed": 0,
                "lease_lost": 0,
                "census": _census(expected=2, complete=2),
            }

    package = ModuleType("research_store")
    package.__path__ = []
    config_module = ModuleType("research_store.config")
    config_module.StoreConfig = StoreConfig
    container_module = ModuleType("research_store.container")
    container_module.build_run_service = lambda _config: SimpleNamespace(
        status=lambda **kwargs: (
            SimpleNamespace(id=run_id, state="indexing")
            if kwargs == {"external_id": "fr_test"}
            else pytest.fail(f"unexpected status lookup: {kwargs}")
        )
    )
    container_module.build_service = lambda _config: corpus_service
    indexing_module = ModuleType("research_store.indexing")
    indexing_module.IndexWorker = IndexWorker
    monkeypatch.setitem(sys.modules, "research_store", package)
    monkeypatch.setitem(sys.modules, "research_store.config", config_module)
    monkeypatch.setitem(sys.modules, "research_store.container", container_module)
    monkeypatch.setitem(sys.modules, "research_store.indexing", indexing_module)

    runner = module._run_scoped_runner("fr_test")
    completed = runner(["research-db", "worker", "--once", "--batch-size", "7"])

    assert completed.returncode == 0
    assert run_batch_calls == [(7, entity_ids)]
    assert len(executions) == 1
    query, params = executions[0]
    assert "FROM research_run_assets" in query
    assert "ORDER BY c.id" in query
    assert params == (
        run_id,
        "parser-v1",
        "normalizer-v1",
        "chunker-v1",
    )

    cancel_state = {"value": False}

    class CancellingCursor(Cursor):
        def fetchall(self) -> list[tuple[UUID]]:
            cancel_state["value"] = True
            return super().fetchall()

    class CancellingConnection:
        def cursor(self) -> CancellingCursor:
            return CancellingCursor()

    class CancellingUnitOfWork(UnitOfWork):
        connection = CancellingConnection()

    corpus_service.uow_factory = lambda: CancellingUnitOfWork()
    with pytest.raises(module.DrainCancelled):
        module._run_scoped_runner(
            "fr_test",
            cancelled=lambda: cancel_state["value"],
        )
    assert run_batch_calls == [(7, entity_ids)]
