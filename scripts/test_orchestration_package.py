"""Regression tests for the research_store.orchestration package (#261).

These tests verify the acceptance criteria for the orchestration
normalization refactor:

A. Import boundary: importing research_store does NOT mutate
   research_store.orchestrator symbol identity.
B. Fresh lifecycle is a single canonical implementation.
C. Resume lifecycle is a single canonical implementation.
D. Facade delegation: ResearchOrchestrator.run delegates to run_research.
E. Facade delegation: ResumableResearchOrchestrator.run delegates to run_resume.
F. Checkpoint delegation: CheckpointResearchOrchestrator delegates to
   orchestration.checkpoint functions.
G. Composition root produces bounded stages.
H. Command dataclass is frozen (immutable).
I. No raw SQL / cursor usage in the orchestration application package.
J. No raw SQL / cursor usage in smart_orchestrator.py.
"""

from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))


class TestImportBoundary:
    """AC-A: importing research_store must not mutate orchestrator symbols."""

    def test_import_research_store_does_not_mutate_orchestrator_identity(self):
        """Verify that importing research_store does not rebind symbols in
        the research_store.orchestrator module.

        Uses a subprocess to avoid polluting the test process module state.
        """
        import subprocess

        script = (
            "import sys, importlib\n"
            "sys.path.insert(0, '.')\n"
            "orch = importlib.import_module('research_store.orchestrator')\n"
            "pre_run = orch.ResearchOrchestrator.run\n"
            "pre_build = orch.ResearchOrchestrator.build.__func__\n"
            "importlib.import_module('research_store')\n"
            "after = sys.modules['research_store.orchestrator']\n"
            "assert after.ResearchOrchestrator.run is pre_run, 'run mutated'\n"
            "assert after.ResearchOrchestrator.build.__func__ is pre_build, 'build mutated'\n"
            "print('OK')\n"
        )
        result = subprocess.run(  # noqa: PLW1510 - intentional non-check for error capture
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(SCRIPTS_DIR),
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Import boundary violated:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestLifecycleUniqueness:
    """AC-B/C: exactly one fresh and one resume lifecycle implementation."""

    def test_run_research_is_single_implementation(self):
        from research_store.orchestration.lifecycle import run_research

        assert callable(run_research)
        assert run_research.__module__ == "research_store.orchestration.lifecycle"

    def test_run_resume_is_single_implementation(self):
        from research_store.orchestration.resume import run_resume

        assert callable(run_resume)
        assert run_resume.__module__ == "research_store.orchestration.resume"

    def test_research_orchestrator_run_delegates_to_run_research(self):
        """ResearchOrchestrator.run is a thin facade delegating to run_research."""
        from research_store.orchestrator import ResearchOrchestrator

        source = inspect.getsource(ResearchOrchestrator.run)
        assert "run_research" in source, (
            "ResearchOrchestrator.run must delegate to run_research"
        )

    def test_resumable_orchestrator_run_delegates_to_run_resume(self):
        """ResumableResearchOrchestrator.run is a thin facade delegating to run_resume."""
        from research_store.smart_orchestrator import ResumableResearchOrchestrator

        source = inspect.getsource(ResumableResearchOrchestrator.run)
        assert "run_resume" in source, (
            "ResumableResearchOrchestrator.run must delegate to run_resume"
        )


class TestCheckpointDelegation:
    """AC-F: CheckpointResearchOrchestrator delegates to orchestration.checkpoint."""

    def test_checkpoint_execute_stage_delegates(self):
        from research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )

        source = inspect.getsource(CheckpointResearchOrchestrator._execute_stage)
        assert "checkpoint_execute_stage" in source

    def test_checkpoint_failed_result_delegates(self):
        from research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )

        source = inspect.getsource(CheckpointResearchOrchestrator._failed_result)
        assert "checkpoint_failed_result" in source


class TestCompositionRoot:
    """AC-G: composition root produces bounded stages."""

    def test_build_production_orchestrator_returns_checkpoint_orchestrator(self):
        from research_store.orchestration.composition import (
            build_production_orchestrator,
        )

        source = inspect.getsource(build_production_orchestrator)
        assert "CheckpointResearchOrchestrator" in source
        assert "BoundedAcquisitionStage" in source
        assert "BoundedExtractionStage" in source

    def test_build_production_orchestrator_injects_bounded_stages(self):
        """The composition root must explicitly pass bounded stage classes."""
        from research_store.orchestration.composition import (
            build_production_orchestrator,
        )

        source = inspect.getsource(build_production_orchestrator)
        assert "acquisition_stage_cls=BoundedAcquisitionStage" in source
        assert "extraction_stage_cls=BoundedExtractionStage" in source


class TestCommandDataclass:
    """AC-H: RunResearchCommand is a frozen (immutable) dataclass."""

    def test_run_research_command_is_frozen(self):
        from research_store.orchestration.commands import RunResearchCommand

        cmd = RunResearchCommand(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            spec={"query": "test"},
            search_plan={"queries": []},
        )
        with pytest.raises(FrozenInstanceError):
            cmd.run_id = UUID("00000000-0000-0000-0000-000000000002")  # type: ignore[misc]

    def test_run_research_command_fields(self):
        from research_store.orchestration.commands import RunResearchCommand

        run_id = UUID("00000000-0000-0000-0000-000000000001")
        cmd = RunResearchCommand(
            run_id=run_id,
            spec={"query": "hello"},
            search_plan={"queries": ["q1"]},
            max_adaptive_cycles=5,
            context={"key": "value"},
        )
        assert cmd.run_id == run_id
        assert cmd.spec == {"query": "hello"}
        assert cmd.search_plan == {"queries": ["q1"]}
        assert cmd.max_adaptive_cycles == 5
        assert cmd.context == {"key": "value"}

    def test_run_research_command_defaults(self):
        from research_store.orchestration.commands import RunResearchCommand

        cmd = RunResearchCommand(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            spec={},
            search_plan={},
        )
        assert cmd.context == {}
        assert cmd.max_adaptive_cycles is None


class TestNoRawSQLInOrchestration:
    """AC-I: No raw SQL / cursor usage in the orchestration application package."""

    @pytest.fixture(scope="class")
    @classmethod
    def orchestration_files(cls) -> list[Path]:
        orch_dir = SCRIPTS_DIR / "research_store" / "orchestration"
        return sorted(orch_dir.glob("*.py"))

    def test_no_cursor_calls(self, orchestration_files: list[Path]):
        """No .cursor() calls in orchestration package files."""
        for path in orchestration_files:
            source = path.read_text()
            assert ".cursor()" not in source, f"{path.name} contains raw .cursor() call"

    def test_no_direct_pg_connection(self, orchestration_files: list[Path]):
        """No direct psycopg/PG connection instantiation in orchestration package."""
        for path in orchestration_files:
            source = path.read_text()
            assert "psycopg.connect" not in source, (
                f"{path.name} contains direct PG connection"
            )
            assert "sqlalchemy" not in source, (
                f"{path.name} contains direct SQLAlchemy usage"
            )

    def test_no_execute_sql_calls(self, orchestration_files: list[Path]):
        """No direct .execute() with SQL strings in orchestration package."""
        sql_keywords = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        for path in orchestration_files:
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                ):
                    continue
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and any(kw in arg.value.upper() for kw in sql_keywords)
                    ):
                        pytest.fail(
                            f"{path.name}:{node.lineno} contains raw SQL execute()"
                        )


class TestNoRawSQLInSmartOrchestrator:
    """AC-J: No raw SQL / cursor usage in smart_orchestrator.py."""

    @pytest.fixture(scope="class")
    @classmethod
    def smart_file(cls) -> Path:
        return SCRIPTS_DIR / "research_store" / "smart_orchestrator.py"

    def test_no_cursor_calls(self, smart_file: Path):
        """No .cursor() calls in smart_orchestrator.py."""
        source = smart_file.read_text()
        assert ".cursor()" not in source, (
            "smart_orchestrator.py contains raw .cursor() call"
        )

    def test_no_direct_pg_connection(self, smart_file: Path):
        """No direct psycopg/PG connection in smart_orchestrator.py."""
        source = smart_file.read_text()
        assert "psycopg.connect" not in source

    def test_no_execute_sql_calls(self, smart_file: Path):
        """No direct .execute() with SQL strings in smart_orchestrator.py."""
        source = smart_file.read_text()
        tree = ast.parse(source)
        sql_keywords = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
            ):
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and any(kw in arg.value.upper() for kw in sql_keywords)
                ):
                    pytest.fail(
                        f"smart_orchestrator.py:{node.lineno} contains raw SQL execute()"
                    )


class TestPublicExports:
    """Verify the orchestration package exposes the expected public API."""

    def test_package_exports(self):
        import research_store.orchestration as orch

        expected = {
            "ResumeCounts",
            "ResumeStatePort",
            "RunResearchCommand",
            "build_production_orchestrator",
            "checkpoint_execute_stage",
            "checkpoint_failed_result",
            "run_research",
            "run_resume",
        }
        actual = set(orch.__all__)
        assert expected == actual, (
            f"Missing: {expected - actual}, Unexpected: {actual - expected}"
        )

    def test_all_exports_are_importable(self):
        import research_store.orchestration as orch

        for name in orch.__all__:
            assert hasattr(orch, name), f"Export '{name}' not found in module"
