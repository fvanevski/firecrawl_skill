"""Static contracts complementing issue #210 production-seam integration tests."""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
STORE = SCRIPTS.parent / "src" / "firecrawl_skill" / "research_store"
PROJECTION = STORE / "retrieval" / "projection"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_checkpoint_migration_is_linear_additive_and_forward_only():
    source = _source(
        STORE / "alembic" / "versions" / "0039_indexing_checkpoints_terminal_guard.py"
    )
    assert 'revision = "0039_index_checkpoint_guard"' in source
    assert 'down_revision = "0038_postgres_authority"' in source
    assert "CREATE TABLE indexing_checkpoints" in source
    assert "CREATE TABLE indexing_checkpoint_observations" in source
    assert "forward repair" in source


def test_terminal_provenance_is_explicit_atomic_and_bidirectional():
    source = _source(
        STORE / "alembic" / "versions" / "0039_indexing_checkpoints_terminal_guard.py"
    )
    assert "UPDATE terminal_decisions" in source
    assert "decision_transaction_id xid8" in source
    assert "transition_transaction_id xid8" in source
    assert "terminal_transition_requires_decision_trigger" in source
    assert "terminal_decision_requires_transition_trigger" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "_terminal_decision_target_state" in source
    assert "ALTER COLUMN reason_code SET DEFAULT" not in source
    assert "ALTER COLUMN state_census SET DEFAULT" not in source


def test_checkpoint_stage_uses_bounded_cancellation_aware_waits():
    source = _source(PROJECTION / "checkpoint_indexing_stage.py")
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "sleep" not in calls
    assert "drain_index_jobs_result" in source
    assert "waiter=cancellation.wait" in source
    assert "cancelled=cancellation.is_set" in source
    assert "IndexCheckpointPending" in source


def test_completed_replay_is_read_only_and_authoritative():
    source = _source(PROJECTION / "index_checkpoint_replay.py")
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "apply_run_transition" not in calls
    assert "_current_membership" in source
    assert "census_index_jobs" in source
    assert "_manifest_count" in source
    assert 'validation_result.get("completion")' in source


def test_public_resume_contract_is_documented_and_exposed():
    frun = _source(SCRIPTS / "frun")
    resume = _source(SCRIPTS / "resume_index_checkpoint.py")
    documentation = _source(
        SCRIPTS.parent / "references" / "indexing-checkpoint-resume.md"
    )
    assert "frun resume <fr_id>" in frun
    assert "replay_completed_checkpoint" in resume
    assert "RESUMABLE_EXIT_CODE" in resume
    assert "Exit code `75`" in documentation
    assert "read-only replay" in documentation
    assert "PostgreSQL" in documentation
    assert "Qdrant" in documentation


def test_orchestrator_resumable_adapter_does_not_terminalize_checkpoint_work():
    source = _source(STORE / "orchestration" / "checkpoint.py")
    assert 'outcome="resumable"' in source
    assert "INDEX_CHECKPOINT_PENDING_PREFIX" in source
    facade = _source(STORE / "checkpoint_orchestrator.py")
    assert "checkpoint_execute_stage" in facade
    assert "checkpoint_failed_result" in facade


def test_wrapper_direct_start_does_not_mutate_or_finalize_checkpoint():
    workflow = _source(STORE / "workflow_service.py")
    checkpoint = _source(STORE / "checkpoint_workflow_service.py")
    begin_section = workflow.split("def complete_operation", 1)[0]
    checkpoint_begin_section = checkpoint.split("def finish_run", 1)[0]
    assert 'status.state != "acquiring"' in begin_section
    assert "frun prepare" in begin_section
    assert "self._transition(" not in begin_section.split("def begin_operation", 1)[1]
    assert "def begin_operation" not in checkpoint
    assert "self._finalize_indexing(" not in checkpoint_begin_section
    assert "self._finalize_indexing(external_run_id" in checkpoint.split(
        "def finish_run", 1
    )[1]


def test_terminal_repository_uses_conflict_safe_idempotent_insert():
    repository_source = _source(STORE / "postgres_terminal.py")
    repository_tree = ast.parse(repository_source)
    string_constants = {
        node.value
        for node in ast.walk(repository_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "ON CONFLICT(run_id,idempotency_key) DO NOTHING" in repository_source
    assert "WHERE run_id=%s AND idempotency_key=%s" in repository_source
    assert "terminal decision idempotency conflict was not readable" in string_constants

    guard_source = _source(STORE / "lifecycle_guard.py")
    assert 'getattr(uow, "terminal_decisions", uow)' in guard_source
    assert "terminal_repository.record_terminal_decision(" in guard_source
    assert "INSERT INTO terminal_decisions" not in guard_source


def test_standalone_terminal_decision_writer_is_fail_closed():
    source = _source(STORE / "terminal_decision_service.py")
    assert "standalone terminal decision persistence is prohibited" in source
    assert "commit_terminal_decision" in source
    assert "INSERT INTO terminal_decisions" not in source


def test_public_run_service_import_remains_checkpoint_guarded():
    from firecrawl_skill.research_store import ResearchRunService
    from firecrawl_skill.research_store.lifecycle_guard import GuardedResearchRunService

    assert ResearchRunService is GuardedResearchRunService
