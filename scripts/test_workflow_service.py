from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_store.invocation_service import InvocationRecord
from research_store.run_service import PERMITTED_TRANSITIONS, RunStatus
from research_store.workflow_service import (
    RunIndexProgress,
    WorkflowBoundaryError,
    WorkflowOperationService,
)


def _run_status(run_id, external_id, state="created", revision=0):
    return RunStatus(
        id=run_id,
        external_id=external_id,
        state=state,
        lifecycle_revision=revision,
        reopened_from_revision=None,
        execution_mode="autonomous_local",
        objective="workflow test",
        declared_outcome=None,
        completed_at=None,
        error=None,
    )


def _invocation(run_id, external_id, operation, status="running"):
    now = datetime.now(timezone.utc)
    return InvocationRecord(
        id=uuid4(),
        run_id=run_id,
        parent_invocation_id=None,
        external_invocation_id=external_id,
        operation=operation,
        status=status,
        lifecycle_revision=0,
        input={},
        output=None,
        error=None,
        metadata={},
        started_at=now,
        completed_at=now if status != "running" else None,
        created_at=now,
    )


class FakeRunService:
    def __init__(self, state="created"):
        self.run_id = uuid4()
        self.external_id = f"fr_{uuid4().hex}"
        self.current = _run_status(self.run_id, self.external_id, state)
        self.transitions = []
        self.uow_factory = lambda: None

    def status(self, *, run_id=None, external_id=None):
        if run_id not in (None, self.run_id) or external_id not in (
            None,
            self.external_id,
        ):
            raise KeyError("run not found")
        return self.current

    def transition(
        self,
        run_id,
        next_state,
        *,
        expected_revision,
        idempotency_key,
        **metadata,
    ):
        assert run_id == self.run_id
        assert expected_revision == self.current.lifecycle_revision
        assert next_state in PERMITTED_TRANSITIONS[self.current.state]
        self.transitions.append(
            (self.current.state, next_state, idempotency_key, metadata)
        )
        self.current = _run_status(
            self.run_id,
            self.external_id,
            next_state,
            self.current.lifecycle_revision + 1,
        )
        return SimpleNamespace()


class FakeInvocationService:
    def __init__(self, run_service):
        self.run_service = run_service
        self.records = {}
        self.complete_calls = 0

    def begin(
        self,
        run_id,
        external_invocation_id,
        operation,
        input_data,
        **_metadata,
    ):
        record = self.records.get(external_invocation_id)
        if record is None:
            record = _invocation(run_id, external_invocation_id, operation)
            record = InvocationRecord(
                **{
                    **record.__dict__,
                    "lifecycle_revision": self.run_service.current.lifecycle_revision,
                    "input": input_data,
                }
            )
            self.records[external_invocation_id] = record
        return record

    def status(self, *, external_invocation_id=None, **_filters):
        if external_invocation_id not in self.records:
            raise KeyError("invocation not found")
        return self.records[external_invocation_id]

    def complete(self, run_id, invocation_id, status, *, output, error, **_metadata):
        self.complete_calls += 1
        current = next(item for item in self.records.values() if item.id == invocation_id)
        completed = InvocationRecord(
            **{
                **current.__dict__,
                "status": "complete" if status == "succeeded" else "failed",
                "output": output,
                "error": error,
                "completed_at": datetime.now(timezone.utc),
            }
        )
        self.records[current.external_invocation_id] = completed
        return completed


class WorkflowServiceHarness(WorkflowOperationService):
    def __init__(self, state="created", progress=None):
        self.fake_run_service = FakeRunService(state)
        self.fake_invocation_service = FakeInvocationService(self.fake_run_service)
        super().__init__(self.fake_run_service, self.fake_invocation_service)
        self.progress = progress or RunIndexProgress(assets=1, chunks=1, pending=0, running=0, failed=0, dead=0, complete=1)

    def index_progress(self, run_id):
        assert run_id == self.fake_run_service.run_id
        return self.progress


def test_fsearch_begin_advances_to_acquiring():
    service = WorkflowServiceHarness()
    invocation_id = f"fc_{uuid4().hex}"

    record = service.begin_operation(
        service.fake_run_service.external_id,
        invocation_id,
        "fsearch",
        {"query": "postgres authority"},
    )

    assert record.status == "running"
    assert service.fake_run_service.current.state == "acquiring"
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "planning",
        "corpus_review",
        "acquiring",
    ]


def test_fscrape_begin_advances_to_extracting():
    service = WorkflowServiceHarness()
    service.begin_operation(
        service.fake_run_service.external_id,
        f"fc_{uuid4().hex}",
        "fscrape",
        {"urls": ["https://example.com"]},
    )
    assert service.fake_run_service.current.state == "extracting"


def test_persisted_completion_advances_to_indexing_and_retry_is_idempotent():
    service = WorkflowServiceHarness()
    invocation_id = f"fc_{uuid4().hex}"
    service.begin_operation(
        service.fake_run_service.external_id,
        invocation_id,
        "fsearch",
        {"query": "postgres authority"},
    )
    output = {"records": [{"persisted": True}]}

    first = service.complete_operation(
        service.fake_run_service.external_id,
        invocation_id,
        succeeded=True,
        output=output,
    )
    second = service.complete_operation(
        service.fake_run_service.external_id,
        invocation_id,
        succeeded=True,
        output=output,
    )

    assert first.status == second.status == "complete"
    assert service.fake_invocation_service.complete_calls == 1
    assert service.fake_run_service.current.state == "indexing"


def test_new_acquisition_after_indexing_requires_complete_index_then_resumes():
    service = WorkflowServiceHarness(state="indexing")
    service.begin_operation(
        service.fake_run_service.external_id,
        f"fc_{uuid4().hex}",
        "fsearch",
        {"query": "more evidence"},
    )
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "coverage_review",
        "acquiring",
    ]


def test_new_acquisition_rejects_incomplete_indexing():
    service = WorkflowServiceHarness(
        state="indexing", progress=RunIndexProgress(assets=1, chunks=2, pending=1, running=0, failed=0, dead=0, complete=1)
    )
    with pytest.raises(WorkflowBoundaryError, match="indexing is incomplete"):
        service.begin_operation(
            service.fake_run_service.external_id,
            f"fc_{uuid4().hex}",
            "fsearch",
            {"query": "more evidence"},
        )


def test_finish_requires_persisted_indexed_assets():
    service = WorkflowServiceHarness(
        state="indexing", progress=RunIndexProgress(assets=1, chunks=2, pending=1, running=0, failed=0, dead=0, complete=1)
    )
    with pytest.raises(WorkflowBoundaryError, match="indexing is not complete"):
        service.finish_run(service.fake_run_service.external_id, outcome="satisfied")


def test_finish_rejects_missing_index_job():
    service = WorkflowServiceHarness(
        state="indexing",
        progress=RunIndexProgress(
            assets=1, chunks=2, pending=0, running=0, failed=0, dead=0, complete=1
        ),
    )
    with pytest.raises(WorkflowBoundaryError, match="indexing is not complete"):
        service.finish_run(service.fake_run_service.external_id, outcome="satisfied")

def test_finish_advances_valid_terminal_path():
    service = WorkflowServiceHarness(state="indexing")
    result = service.finish_run(
        service.fake_run_service.external_id,
        outcome="satisfied",
        source_manifest_sha256="a" * 64,
        answer_sha256="b" * 64,
    )
    assert result.state == "completed"
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "coverage_review",
        "synthesizing",
        "validating",
        "completed",
    ]


def test_partial_finish_stops_at_partial():
    service = WorkflowServiceHarness(state="indexing")
    result = service.finish_run(
        service.fake_run_service.external_id,
        outcome="partial",
    )
    assert result.state == "partial"


def test_failed_finish_uses_permitted_path_from_created():
    service = WorkflowServiceHarness(state="created")
    result = service.finish_run(
        service.fake_run_service.external_id,
        outcome="smoke test failed",
        status_name="failed",
    )
    assert result.state == "failed"
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "planning",
        "failed",
    ]
