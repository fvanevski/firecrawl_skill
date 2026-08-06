from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
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
        self.begin_calls = 0
        self.complete_calls = 0

    def begin(
        self,
        run_id,
        external_invocation_id,
        operation,
        input_data,
        **_metadata,
    ):
        self.begin_calls += 1
        record = self.records.get(external_invocation_id)
        if record is None:
            now = datetime.now(timezone.utc)
            current = self.run_service.current
            record = SimpleNamespace(
                id=uuid4(),
                run_id=run_id,
                external_invocation_id=external_invocation_id,
                operation=operation,
                status="running",
                lifecycle_revision=current.lifecycle_revision,
                input=input_data,
                output=None,
                error=None,
                metadata={
                    "lifecycle_state": current.state,
                    "lifecycle_revision": current.lifecycle_revision,
                },
                started_at=now,
                completed_at=None,
                created_at=now,
            )
            self.records[external_invocation_id] = record
        return record

    def status(self, *, external_invocation_id=None, **_filters):
        if external_invocation_id not in self.records:
            raise KeyError("invocation not found")
        return self.records[external_invocation_id]

    def complete(self, run_id, invocation_id, status, *, output, error, **_metadata):
        self.complete_calls += 1
        current = next(
            item for item in self.records.values() if item.id == invocation_id
        )
        completed = SimpleNamespace(
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
        self.progress = progress or RunIndexProgress(
            assets=1, chunks=1, pending=0, running=0, failed=0, dead=0, complete=1
        )

    def index_progress(self, run_id):
        assert run_id == self.fake_run_service.run_id
        return self.progress


def test_direct_begin_requires_explicit_prepare_without_mutation():
    service = WorkflowServiceHarness()

    with pytest.raises(WorkflowBoundaryError, match="frun prepare"):
        service.begin_operation(
            service.fake_run_service.external_id,
            f"fc_{uuid4().hex}",
            "fsearch",
            {"query": "postgres authority"},
        )

    assert service.fake_run_service.transitions == []
    assert service.fake_invocation_service.begin_calls == 0


def test_prepare_advances_once_to_acquiring_and_is_idempotent():
    service = WorkflowServiceHarness()

    first = service.prepare_run(service.fake_run_service.external_id)
    second = service.prepare_run(service.fake_run_service.external_id)

    assert first.state == second.state == "acquiring"
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "planning",
        "corpus_review",
        "acquiring",
    ]


@pytest.mark.parametrize("operation", ["fsearch", "fscrape"])
def test_prepared_direct_invocation_records_exact_state_and_revision(operation):
    service = WorkflowServiceHarness()
    prepared = service.prepare_run(service.fake_run_service.external_id)
    transitions_before = list(service.fake_run_service.transitions)

    record = service.begin_operation(
        service.fake_run_service.external_id,
        f"fc_{uuid4().hex}",
        operation,
        {"query": "postgres authority"},
    )

    assert record.status == "running"
    assert record.lifecycle_revision == prepared.lifecycle_revision
    assert record.metadata["lifecycle_state"] == "acquiring"
    assert record.metadata["lifecycle_revision"] == prepared.lifecycle_revision
    assert service.fake_run_service.transitions == transitions_before


def test_completion_is_idempotent_and_does_not_advance_lifecycle():
    service = WorkflowServiceHarness()
    service.prepare_run(service.fake_run_service.external_id)
    invocation_id = f"fc_{uuid4().hex}"
    service.begin_operation(
        service.fake_run_service.external_id,
        invocation_id,
        "fscrape",
        {"urls": ["https://example.com"]},
    )
    transitions_before = list(service.fake_run_service.transitions)
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
    assert service.fake_run_service.current.state == "acquiring"
    assert service.fake_run_service.transitions == transitions_before


def test_seal_acquisition_is_explicit_and_idempotent():
    service = WorkflowServiceHarness()
    service.prepare_run(service.fake_run_service.external_id)

    first = service.seal_acquisition(service.fake_run_service.external_id)
    second = service.seal_acquisition(service.fake_run_service.external_id)

    assert first.state == second.state == "indexing"
    assert [item[1] for item in service.fake_run_service.transitions] == [
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
    ]


def test_direct_acquisition_rejects_indexing_without_checkpoint_side_effects():
    service = WorkflowServiceHarness(state="indexing")

    with pytest.raises(WorkflowBoundaryError, match="state indexing"):
        service.begin_operation(
            service.fake_run_service.external_id,
            f"fc_{uuid4().hex}",
            "fsearch",
            {"query": "more evidence"},
        )

    assert service.fake_run_service.transitions == []
    assert service.fake_invocation_service.begin_calls == 0


def test_finish_requires_persisted_indexed_assets():
    service = WorkflowServiceHarness(
        state="indexing",
        progress=RunIndexProgress(
            assets=1, chunks=2, pending=1, running=0, failed=0, dead=0, complete=1
        ),
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


def test_finish_advances_valid_terminal_path_and_retry_is_idempotent():
    service = WorkflowServiceHarness(state="indexing")
    first = service.finish_run(
        service.fake_run_service.external_id,
        outcome="satisfied",
        source_manifest_sha256="a" * 64,
        answer_sha256="b" * 64,
    )
    second = service.finish_run(
        service.fake_run_service.external_id,
        outcome="satisfied",
        source_manifest_sha256="a" * 64,
        answer_sha256="b" * 64,
    )
    assert first.state == second.state == "completed"
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
