"""Exercise the canonical RC-9 lifecycle and transaction documentation."""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from research_store.invocation_service import InvocationService
from research_store.run_service import ResearchRunService

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SKILL_ROOT = SCRIPTS.parent
RUN_ID = "fr_" + "a" * 32
INVOCATION_ID = "fc_" + "b" * 32

DRAIN_DOCUMENTS = (
    "README.md",
    "SKILL.md",
    "references/authoritative-workflows.md",
    "references/operations-runbook.md",
    "references/research-store-operations.md",
    "references/recovery-drill-checklist.md",
    "references/release-notes-rc9.md",
)


def _load_drain_module():
    path = SCRIPTS / "drain_index_jobs.py"
    spec = importlib.util.spec_from_file_location("drain_index_jobs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canonical_workflow_reference_exists() -> None:
    path = SKILL_ROOT / "references/authoritative-workflows.md"
    assert path.is_file()
    assert path.stat().st_size > 0


@pytest.mark.parametrize("rel_path", DRAIN_DOCUMENTS)
def test_runtime_workflows_use_canonical_drain_helper(rel_path: str) -> None:
    content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
    assert "drain_index_jobs.py" in content


def test_canonical_workflow_orders_drain_before_finish_and_activation() -> None:
    content = (SKILL_ROOT / "references/authoritative-workflows.md").read_text(
        encoding="utf-8"
    )
    acquire = content.index("scripts/fsearch 'bounded query'")
    first_drain = content.index("python3 scripts/drain_index_jobs.py", acquire)
    run_status = content.index("scripts/research-db run-status", first_drain)
    finish = content.index("scripts/frun finish", run_status)
    assert acquire < first_drain < run_status < finish

    build = content.index("scripts/research-db index-build")
    rebuild_drain = content.index("python3 scripts/drain_index_jobs.py", build)
    reconcile = content.index("scripts/research-db reconcile-qdrant", rebuild_drain)
    activate = content.index("scripts/research-db index-activate", reconcile)
    assert build < rebuild_drain < reconcile < activate


def test_architecture_matches_blob_first_transaction() -> None:
    architecture = (SKILL_ROOT / "references/research-store-architecture.md").read_text(
        encoding="utf-8"
    )
    blob = architecture.index("durably install immutable payload bytes")
    transaction = architecture.index("PostgreSQL acquisition transaction")
    commit = architecture.index("commit PostgreSQL metadata")
    returned = architecture.index("return bounded stable authoritative IDs")
    assert blob < transaction < commit < returned
    assert "A failed blob write creates no authoritative PostgreSQL record" in (
        architecture
    )
    assert "an orphan, not a corpus record" in architecture

    from research_store.service import CorpusService

    prepare_source = inspect.getsource(CorpusService._prepare_ingest)
    ingest_source = inspect.getsource(CorpusService.ingest)
    assert "self.blob_store.put" in prepare_source
    assert ingest_source.index("self._prepare_ingest") < ingest_source.index(
        "persist_ingest"
    )


def test_drain_helper_example_parses() -> None:
    module = _load_drain_module()
    args = module.build_parser().parse_args(
        ["--batch-size", "64", "--max-batches", "1000"]
    )
    assert args.batch_size == 64
    assert args.max_batches == 1000


def test_worker_once_processes_exactly_one_batch() -> None:
    from research_store.indexing import IndexWorker

    worker = IndexWorker(lambda: None, None, None)
    calls: list[int] = []

    def run_batch(limit: int):
        calls.append(limit)
        return {
            "claimed": limit,
            "complete": limit,
            "failed": 0,
            "lease_lost": 0,
        }

    cast(Any, worker).run_batch = run_batch
    result = worker.run_forever(
        batch_size=64,
        once=True,
        install_signal_handlers=False,
    )
    assert calls == [64]
    assert result["batches"] == 1
    assert result["claimed"] == 64


def test_drain_helper_processes_multiple_batches_until_empty() -> None:
    module = _load_drain_module()
    outputs = iter(
        [
            {"claimed": 64, "failed": 0, "lease_lost": 0},
            {"claimed": 64, "failed": 0, "lease_lost": 0},
            {"claimed": 1, "failed": 0, "lease_lost": 0},
            {"claimed": 0, "failed": 0, "lease_lost": 0},
        ]
    )
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        payload = next(outputs)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    result = module.drain_index_jobs(
        Path("/tmp/research-db"),
        batch_size=64,
        max_batches=10,
        runner=runner,
    )
    assert result == 0
    assert len(calls) == 4
    assert all(call[-2:] == ["--batch-size", "64"] for call in calls)


def test_drain_helper_fails_on_authoritative_item_failure() -> None:
    module = _load_drain_module()

    def runner(argv):
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"claimed": 1, "failed": 1, "lease_lost": 0}',
            "",
        )

    assert (
        module.drain_index_jobs(
            Path("/tmp/research-db"),
            runner=runner,
        )
        == 1
    )


def test_drain_helper_fails_on_lease_loss() -> None:
    module = _load_drain_module()

    def runner(argv):
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"claimed": 1, "failed": 0, "lease_lost": 1}',
            "",
        )

    assert (
        module.drain_index_jobs(
            Path("/tmp/research-db"),
            runner=runner,
        )
        == 1
    )


def test_drain_helper_fails_at_max_batch_bound() -> None:
    module = _load_drain_module()

    def runner(argv):
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"claimed": 1, "failed": 0, "lease_lost": 0}',
            "",
        )

    assert (
        module.drain_index_jobs(
            Path("/tmp/research-db"),
            max_batches=2,
            runner=runner,
        )
        == 1
    )


def test_workflow_rejects_finish_and_direct_followup_during_indexing() -> None:
    from research_store.workflow_service import (
        RunIndexProgress,
        WorkflowBoundaryError,
        WorkflowOperationService,
    )

    run_id = UUID(int=1)
    service = WorkflowOperationService.__new__(WorkflowOperationService)
    service.run_service = cast(
        ResearchRunService,
        SimpleNamespace(
            status=lambda **_kwargs: SimpleNamespace(id=run_id, state="indexing")
        ),
    )
    service.invocation_service = cast(InvocationService, SimpleNamespace())
    progress_calls = 0

    def incomplete_progress(_run_id):
        nonlocal progress_calls
        progress_calls += 1
        return RunIndexProgress(
            assets=1,
            chunks=2,
            pending=1,
            running=0,
            failed=0,
            dead=0,
            complete=1,
        )

    cast(Any, service).index_progress = incomplete_progress

    with pytest.raises(
        WorkflowBoundaryError,
        match="cannot begin fscrape.*state indexing.*frun prepare",
    ):
        service.begin_operation(RUN_ID, INVOCATION_ID, "fscrape", {})
    assert progress_calls == 0

    with pytest.raises(
        WorkflowBoundaryError,
        match="run indexing is not complete",
    ):
        service.finish_run(RUN_ID, outcome="satisfied")
    assert progress_calls == 1


def test_failed_blob_write_never_opens_metadata_transaction() -> None:
    from research_store.service import CorpusService, IngestRequest

    class FailingBlobStore:
        def put(self, *_args, **_kwargs):
            raise OSError("injected blob failure")

    database_calls = 0

    def uow_factory():
        nonlocal database_calls
        database_calls += 1
        raise AssertionError("metadata transaction must not start")

    service = CorpusService.__new__(CorpusService)
    service.blob_store = FailingBlobStore()
    service.uow_factory = uow_factory

    request = IngestRequest(
        requested_url="https://example.com",
        content=b"payload",
        mime_type="text/plain",
    )
    with pytest.raises(OSError, match="injected blob failure"):
        service.ingest(request)
    assert database_calls == 0
