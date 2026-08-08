"""Regression contracts for issue #212 curated direct-acquisition lifecycle."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from research_store.curated_run_service import CuratedRunError, CuratedRunService
from research_store.direct_invocation_service import DirectInvocationService
from research_store.invocation_service import InvocationError
from research_store.run_service import PERMITTED_TRANSITIONS, RunStatus
from research_store.workflow_service import RunIndexProgress, WorkflowOperationService

SCRIPTS = Path(__file__).resolve().parent
STORE = SCRIPTS / "research_store"


def _status(run_id, external_id, state="created", revision=0):
    return RunStatus(
        id=run_id,
        external_id=external_id,
        state=state,
        lifecycle_revision=revision,
        reopened_from_revision=None,
        execution_mode="autonomous_local",
        objective="curated issue 212 test",
        declared_outcome=None,
        completed_at=None,
        error=None,
    )


class _RunService:
    def __init__(self):
        self.run_id = uuid4()
        self.external_id = f"fr_{uuid4().hex}"
        self.current = _status(self.run_id, self.external_id)
        self.transitions: list[str] = []
        self.run_mode = "curated"
        self.uow_factory = lambda: None

    def create(self, objective, external_id, *, metadata, **_options):
        assert objective
        assert metadata == {"run_mode": "curated"}
        self.external_id = external_id
        self.current = _status(self.run_id, external_id)
        return self.current

    def status(self, *, run_id=None, external_id=None):
        assert run_id in (None, self.run_id)
        assert external_id in (None, self.external_id)
        return self.current

    def transition(self, run_id, next_state, *, expected_revision, **_metadata):
        assert run_id == self.run_id
        assert expected_revision == self.current.lifecycle_revision
        assert next_state in PERMITTED_TRANSITIONS[self.current.state]
        self.transitions.append(next_state)
        self.current = _status(
            self.run_id,
            self.external_id,
            next_state,
            self.current.lifecycle_revision + 1,
        )


class _InvocationService:
    def __init__(self, runs):
        self.runs = runs
        self.records: dict[str, SimpleNamespace] = {}

    def begin(self, run_id, external_id, operation, input_data, **_metadata):
        current = self.runs.current
        record = self.records.get(external_id)
        if record is None:
            record = SimpleNamespace(
                id=uuid4(),
                run_id=run_id,
                external_invocation_id=external_id,
                operation=operation,
                status="running",
                lifecycle_revision=current.lifecycle_revision,
                metadata={
                    "lifecycle_state": current.state,
                    "lifecycle_revision": current.lifecycle_revision,
                },
                input=input_data,
                output=None,
                error=None,
            )
            self.records[external_id] = record
        return record

    def status(self, *, external_invocation_id, **_filters):
        return self.records[external_invocation_id]

    def complete(self, _run_id, invocation_id, status, *, output, error, **_metadata):
        record = next(
            item for item in self.records.values() if item.id == invocation_id
        )
        record.status = "complete" if status == "succeeded" else "failed"
        record.output = output
        record.error = error
        return record


class _LifecycleCompletion:
    source_manifest_sha256 = "a" * 64
    answer_sha256 = "b" * 64

    def completion_fields(self):
        return {
            "source_manifest_sha256": self.source_manifest_sha256,
            "answer_sha256": self.answer_sha256,
            "provenance_type": "authoritative",
            "completion_provenance": {
                "schema_version": "completion-provenance-v1",
                "source": "issue-212-lifecycle-unit-fixture",
            },
        }


class _Workflow(WorkflowOperationService):
    def __init__(self, runs, invocations):
        super().__init__(runs, invocations)
        self.progress = RunIndexProgress(
            assets=4,
            chunks=4,
            pending=0,
            running=0,
            failed=0,
            dead=0,
            complete=4,
        )

    def index_progress(self, run_id):
        assert run_id == self.run_service.run_id
        return self.progress

    def _assert_completion_gates(self, run_id, **_assertions):
        assert run_id == self.run_service.run_id
        return _LifecycleCompletion()


class _PromotionService:
    def __init__(self, runs, subjects, *, fail_seal_once=False):
        self.runs = runs
        self.stages = {subject: "extracted" for subject in subjects}
        self.seal = None
        self.prepare_calls = 0
        self.fail_seal_once = fail_seal_once
        self.failed_seal = False

    def promote(self, subject_id, target_stage, **metadata):
        assert metadata["expected_run_id"] == self.runs.run_id
        assert self.runs.current.state == "acquiring"
        current = self.stages[subject_id]
        if current == target_stage:
            return {"id": subject_id, "current_stage": current}
        assert current == "extracted"
        assert target_stage == "retained"
        self.stages[subject_id] = target_stage
        return {"id": subject_id, "current_stage": target_stage}

    def reject(self, subject_id, **metadata):
        assert metadata["expected_run_id"] == self.runs.run_id
        assert self.runs.current.state == "acquiring"
        self.stages[subject_id] = "rejected"
        return {"id": subject_id, "current_stage": "rejected"}

    def prepare_for_indexing(self, run_id, *, lifecycle_revision, **_metadata):
        assert run_id == self.runs.run_id
        assert self.runs.current.state == "indexing"
        assert lifecycle_revision == self.runs.current.lifecycle_revision
        assert set(self.stages.values()) == {"retained"}
        self.prepare_calls += 1
        if self.fail_seal_once and not self.failed_seal:
            self.failed_seal = True
            raise RuntimeError("injected membership seal interruption")
        if self.seal is None:
            self.seal = SimpleNamespace(
                id=uuid4(),
                seal_revision=1,
                membership_sha256="a" * 64,
                expected_asset_count=len(self.stages),
                expected_chunk_count=len(self.stages),
            )
        return self.seal

    def get_active_seal(self, run_id):
        assert run_id == self.runs.run_id
        return self.seal


class _CuratedService(CuratedRunService):
    def mode(self, run_id: UUID) -> str:
        assert run_id == self.run_service.run_id
        return self.run_service.run_mode


def _service(*, subject_count=4, fail_seal_once=False):
    runs = _RunService()
    invocations = _InvocationService(runs)
    workflow = _Workflow(runs, invocations)
    subjects = [uuid4() for _ in range(subject_count)]
    promotions = _PromotionService(
        runs,
        subjects,
        fail_seal_once=fail_seal_once,
    )
    return (
        runs,
        workflow,
        subjects,
        promotions,
        _CuratedService(runs, workflow, promotions),
    )


def test_curated_four_asset_flow_completes_without_smart_expansion():
    runs, workflow, subjects, promotions, service = _service()

    started = service.start("four AP assets", runs.external_id, run_mode="curated")
    assert started.run_mode == "curated"
    prepared = service.prepare(runs.external_id)
    assert prepared.run.state == "acquiring"

    search_id = f"fc_{uuid4().hex}"
    search = workflow.begin_operation(
        runs.external_id,
        search_id,
        "fsearch",
        {"query": "four exact assets"},
    )
    workflow.complete_operation(
        runs.external_id,
        search_id,
        succeeded=True,
        output={"records": [{"persisted": True}]},
    )
    scrape_id = f"fc_{uuid4().hex}"
    scrape = workflow.begin_operation(
        runs.external_id,
        scrape_id,
        "fscrape",
        {"urls": [f"https://example.test/{index}" for index in range(4)]},
    )
    workflow.complete_operation(
        runs.external_id,
        scrape_id,
        succeeded=True,
        output={"records": [{"persisted": True} for _ in range(4)]},
    )

    assert search.metadata["lifecycle_state"] == "acquiring"
    assert scrape.metadata["lifecycle_state"] == "acquiring"
    assert search.lifecycle_revision == scrape.lifecycle_revision == 3
    for subject in subjects:
        service.retain(runs.external_id, subject)

    first_seal = service.seal_acquisition(runs.external_id)
    second_seal = service.seal_acquisition(runs.external_id)
    assert first_seal["seal_id"] == second_seal["seal_id"]
    assert first_seal["expected_asset_count"] == 4
    assert promotions.prepare_calls == 2

    first_finish = service.finish(runs.external_id, outcome="satisfied")
    second_finish = service.finish(runs.external_id, outcome="satisfied")
    assert first_finish.run.state == second_finish.run.state == "completed"
    assert service.resume(runs.external_id)["next_action"] == "none"
    assert runs.transitions == [
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
        "coverage_review",
        "synthesizing",
        "validating",
        "completed",
    ]


def test_interrupted_seal_resumes_sealing_before_checkpoint():
    runs, _workflow, subjects, promotions, service = _service(
        subject_count=1,
        fail_seal_once=True,
    )
    service.start("recoverable curated seal", runs.external_id, run_mode="curated")
    service.prepare(runs.external_id)
    service.retain(runs.external_id, subjects[0])

    with pytest.raises(RuntimeError, match="seal interruption"):
        service.seal_acquisition(runs.external_id)

    assert runs.current.state == "indexing"
    assert promotions.get_active_seal(runs.run_id) is None
    resume = service.resume(runs.external_id)
    assert resume["membership_sealed"] is False
    assert resume["next_action"] == f"frun seal-acquisition {runs.external_id}"
    with pytest.raises(CuratedRunError, match="no active completion membership"):
        service.finish(runs.external_id, outcome="satisfied")

    repaired = service.seal_acquisition(runs.external_id)
    assert repaired["state"] == "indexing"
    assert repaired["expected_asset_count"] == 1
    assert runs.transitions.count("extracting") == 1
    assert runs.transitions.count("indexing") == 1
    assert service.resume(runs.external_id)["next_action"] == (
        "resume index checkpoint"
    )


def test_curated_commands_reject_autonomous_or_legacy_modes():
    runs, workflow, subjects, promotions, _service_value = _service(subject_count=1)
    runs.run_mode = "autonomous"
    service = _CuratedService(runs, workflow, promotions)

    with pytest.raises(CuratedRunError, match="not curated"):
        service.retain(runs.external_id, subjects[0])


class _DirectCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.connection.statements.append((normalized, params))
        if normalized.startswith("SELECT state,lifecycle_revision"):
            assert "FOR SHARE" in normalized
            self.row = (self.connection.state, self.connection.revision)
        elif normalized.startswith("SELECT id,run_id,operation,status,input"):
            self.row = None
        elif normalized.startswith("INSERT INTO research_invocations"):
            self.connection.insert_metadata = json.loads(params[-1])
            self.row = (self.connection.invocation_id,)
        else:
            raise AssertionError(normalized)

    def fetchone(self):
        return self.row


class _DirectRuns:
    def __init__(self, connection):
        self.connection = connection

    def append_event(self, *args, **kwargs):
        self.connection.event = (args, kwargs)

    def get_invocation_status(self, *, invocation_id):
        assert invocation_id == self.connection.invocation_id
        now = datetime.now(timezone.utc)
        return {
            "id": invocation_id,
            "run_id": self.connection.run_id,
            "parent_invocation_id": None,
            "external_invocation_id": "fc_direct",
            "operation": "fsearch",
            "status": "running",
            "lifecycle_revision": self.connection.revision,
            "input": {"query": "exact"},
            "output": None,
            "error": None,
            "metadata": self.connection.insert_metadata,
            "started_at": now,
            "completed_at": None,
            "created_at": now,
        }


class _DirectConnection:
    def __init__(self, *, state="acquiring", revision=7):
        self.run_id = uuid4()
        self.invocation_id = uuid4()
        self.state = state
        self.revision = revision
        self.statements = []
        self.insert_metadata = None
        self.event = None

    def cursor(self):
        return _DirectCursor(self)


class _DirectUow:
    def __init__(self, connection):
        self.connection = connection
        self.runs = _DirectRuns(connection)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_direct_invocation_locks_and_persists_exact_state_revision():
    connection = _DirectConnection()
    service = DirectInvocationService(lambda: _DirectUow(connection))

    record = service.begin(
        connection.run_id,
        "fc_direct",
        "fsearch",
        {"query": "exact"},
        actor_type="wrapper",
    )

    assert record.lifecycle_revision == 7
    assert record.metadata["lifecycle_state"] == "acquiring"
    assert record.metadata["lifecycle_revision"] == 7
    select_index = next(
        index
        for index, (statement, _params) in enumerate(connection.statements)
        if statement.startswith("SELECT state,lifecycle_revision")
    )
    insert_index = next(
        index
        for index, (statement, _params) in enumerate(connection.statements)
        if statement.startswith("INSERT INTO research_invocations")
    )
    assert select_index < insert_index
    assert connection.event[1]["payload"]["lifecycle_state"] == "acquiring"
    assert connection.event[1]["payload"]["lifecycle_revision"] == 7


def test_direct_invocation_rejects_locked_ineligible_state_without_side_effects():
    connection = _DirectConnection(state="indexing")
    service = DirectInvocationService(lambda: _DirectUow(connection))

    with pytest.raises(InvocationError, match="state indexing"):
        service.begin(
            connection.run_id,
            "fc_direct",
            "fsearch",
            {"query": "exact"},
            actor_type="wrapper",
        )

    assert not any(
        statement.startswith("INSERT INTO research_invocations")
        for statement, _params in connection.statements
    )
    assert connection.event is None


def test_public_contract_wires_production_boundaries_and_no_smart_expansion():
    frun = (SCRIPTS / "frun").read_text(encoding="utf-8")
    service = (STORE / "curated_run_service.py").read_text(encoding="utf-8")
    invocation = (STORE / "direct_invocation_service.py").read_text(encoding="utf-8")
    container = (STORE / "container.py").read_text(encoding="utf-8")
    scrape = (STORE / "direct_scrape_service.py").read_text(encoding="utf-8")
    for command in ("--run-mode", "prepare", "retain", "reject", "seal-acquisition"):
        assert command in frun
    assert "--mode" in frun
    assert "legacy_unspecified" in service
    assert "get_active_seal" in service
    assert "FOR SHARE" in invocation
    assert "DirectInvocationService" in container
    assert '"lifecycle_state": lifecycle_state' in scrape
    assert "smart_search" not in service
    assert "smart expansion" not in service.lower()
    workflow = (
        SCRIPTS.parent / ".github" / "workflows" / "index-checkpoint.yml"
    ).read_text(encoding="utf-8")
    assert "scripts/test_curated_run_lifecycle.py" in workflow
    assert "scripts/test_curated_run_integration.py" in workflow
    for source in (service, invocation):
        calls = {
            node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "sleep" not in calls
