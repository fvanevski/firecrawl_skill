"""Static contract tests for issue #210 checkpoint and terminal guards."""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
STORE = SCRIPTS / "research_store"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_checkpoint_migration_is_linear_additive_and_forward_only():
    source = _source(
        STORE
        / "alembic"
        / "versions"
        / "0039_indexing_checkpoints_terminal_guard.py"
    )
    assert 'revision = "0039_indexing_checkpoints_terminal_guard"' in source
    assert 'down_revision = "0038_postgres_authority"' in source
    assert "CREATE TABLE indexing_checkpoints" in source
    assert "CREATE TABLE indexing_checkpoint_observations" in source
    assert "terminal_transition_requires_decision" in source
    assert "legacy_unstructured" in source
    assert "forward repair" in source


def test_checkpoint_stage_uses_bounded_cancellation_aware_waits():
    path = STORE / "checkpoint_indexing_stage.py"
    source = _source(path)
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


def test_public_resume_contract_is_documented_and_exposed():
    frun = _source(SCRIPTS / "frun")
    resume = _source(SCRIPTS / "resume_index_checkpoint.py")
    documentation = _source(
        SCRIPTS.parent / "references" / "indexing-checkpoint-resume.md"
    )
    assert "frun resume <fr_id>" in frun
    assert "RESUMABLE_EXIT_CODE" in resume
    assert "Exit code `75`" in documentation
    assert "PostgreSQL" in documentation
    assert "Qdrant" in documentation


def test_orchestrator_resumable_adapter_does_not_terminalize_checkpoint_work():
    source = _source(STORE / "checkpoint_orchestrator.py")
    assert 'outcome="resumable"' in source
    assert "super()._failed_result" in source
    assert "INDEX_CHECKPOINT_PENDING_PREFIX" in source


def test_wrapper_finish_uses_sealed_checkpoint_adapter():
    source = _source(STORE / "checkpoint_workflow_service.py")
    assert "service.ensure(" in source
    assert "service.finalize(" in source
    assert "super().finish_run" in source
    assert "run indexing remains recoverable" in source
