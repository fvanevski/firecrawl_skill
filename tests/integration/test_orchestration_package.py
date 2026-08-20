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
  K. PostgresResumeStateReader owns no SQL / cursors / connections.
 L. PostgresResumeStateReader live round-trip over the canonical repos.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID, uuid4

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


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
            "from pathlib import Path\n"
            "root = Path.cwd().parent\n"
            "sys.path.insert(0, str(root / 'src'))\n"
            "sys.path.insert(0, str(root / 'scripts'))\n"
            "orch = importlib.import_module('firecrawl_skill.research_store.orchestrator')\n"
            "pre_run = orch.ResearchOrchestrator.run\n"
            "pre_build = orch.ResearchOrchestrator.build.__func__\n"
            "importlib.import_module('firecrawl_skill.research_store')\n"
            "after = sys.modules['firecrawl_skill.research_store.orchestrator']\n"
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
        from firecrawl_skill.research_store.orchestration.lifecycle import run_research

        assert callable(run_research)
        assert (
            run_research.__module__
            == "firecrawl_skill.research_store.orchestration.lifecycle"
        )

    def test_run_resume_is_single_implementation(self):
        from firecrawl_skill.research_store.orchestration.resume import run_resume

        assert callable(run_resume)
        assert (
            run_resume.__module__
            == "firecrawl_skill.research_store.orchestration.resume"
        )

    def test_research_orchestrator_run_delegates_to_run_research(self):
        """ResearchOrchestrator.run is a thin facade delegating to run_research."""
        from firecrawl_skill.research_store.orchestrator import ResearchOrchestrator

        source = inspect.getsource(ResearchOrchestrator.run)
        assert "run_research" in source, (
            "ResearchOrchestrator.run must delegate to run_research"
        )

    def test_resumable_orchestrator_run_delegates_to_run_resume(self):
        """ResumableResearchOrchestrator.run is a thin facade delegating to run_resume."""
        from firecrawl_skill.research_store.smart_orchestrator import (
            ResumableResearchOrchestrator,
        )

        source = inspect.getsource(ResumableResearchOrchestrator.run)
        assert "run_resume" in source, (
            "ResumableResearchOrchestrator.run must delegate to run_resume"
        )


class TestCommandDelegation:
    """Facades must hand an actual ``RunResearchCommand`` to the use cases.

    These are behavioral regressions, not source-string inspection: the
    canonical use case is patched and the facade is driven with real input,
    then the argument the use case actually received is asserted to be a
    ``RunResearchCommand`` carrying the caller's values.
    """

    def test_run_research_receives_command(self, monkeypatch):
        from firecrawl_skill.research_store.orchestration import lifecycle
        from firecrawl_skill.research_store.orchestration.commands import (
            RunResearchCommand,
        )
        from firecrawl_skill.research_store.orchestrator import ResearchOrchestrator

        captured: dict = {}
        sentinel = object()

        def fake_run_research(orchestrator, command):
            captured["command"] = command
            return sentinel

        monkeypatch.setattr(lifecycle, "run_research", fake_run_research)

        run_id = UUID("00000000-0000-0000-0000-000000000001")
        result = ResearchOrchestrator.run(
            cast(ResearchOrchestrator, object()),
            run_id,
            {"query": "q"},
            {"queries": ["q1"]},
            max_adaptive_cycles=3,
            context={"k": "v"},
        )
        assert result is sentinel
        command = captured["command"]
        assert isinstance(command, RunResearchCommand)
        assert command.run_id == run_id
        assert command.spec == {"query": "q"}
        assert command.search_plan == {"queries": ["q1"]}
        assert command.max_adaptive_cycles == 3
        assert command.context == {"k": "v"}

    def test_run_resume_receives_command(self, monkeypatch):
        from types import SimpleNamespace

        import firecrawl_skill.research_store.orchestration.resume as resume_mod
        from firecrawl_skill.research_store.orchestration.commands import (
            RunResearchCommand,
        )
        from firecrawl_skill.research_store.resume_state_repository import (
            PostgresResumeStateReader,
        )
        from firecrawl_skill.research_store.smart_orchestrator import (
            ResumableResearchOrchestrator,
        )

        captured: dict = {}
        sentinel = object()

        def fake_run_resume(orchestrator, command, *, state_port):
            captured["command"] = command
            captured["state_port"] = state_port
            return sentinel

        monkeypatch.setattr(resume_mod, "run_resume", fake_run_resume)

        orchestrator = SimpleNamespace(
            run_service=SimpleNamespace(uow_factory=lambda: None)
        )
        run_id = UUID("00000000-0000-0000-0000-000000000001")
        result = ResumableResearchOrchestrator.run(
            cast(ResumableResearchOrchestrator, orchestrator),
            run_id,
            {"query": "q"},
            {"queries": ["q1"]},
            max_adaptive_cycles=4,
            context={"k": "v"},
        )
        assert result is sentinel
        command = captured["command"]
        assert isinstance(command, RunResearchCommand)
        assert command.run_id == run_id
        assert command.spec == {"query": "q"}
        assert command.search_plan == {"queries": ["q1"]}
        assert command.max_adaptive_cycles == 4
        assert command.context == {"k": "v"}
        assert isinstance(captured["state_port"], PostgresResumeStateReader)


class TestCheckpointDelegation:
    """AC-F: CheckpointResearchOrchestrator delegates to orchestration.checkpoint."""

    def test_checkpoint_execute_stage_delegates(self):
        from firecrawl_skill.research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )

        source = inspect.getsource(CheckpointResearchOrchestrator._execute_stage)
        assert "checkpoint_execute_stage" in source

    def test_checkpoint_failed_result_delegates(self):
        from firecrawl_skill.research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )

        source = inspect.getsource(CheckpointResearchOrchestrator._failed_result)
        assert "checkpoint_failed_result" in source


class TestCompositionRoot:
    """AC-G: composition root produces bounded stages."""

    def test_build_production_orchestrator_returns_checkpoint_orchestrator(self):
        from firecrawl_skill.research_store.orchestration.composition import (
            build_production_orchestrator,
        )

        source = inspect.getsource(build_production_orchestrator)
        assert "CheckpointResearchOrchestrator" in source
        assert "BoundedAcquisitionStage" in source
        assert "ProductionBoundedExtractionStage" in source

    def test_build_production_orchestrator_injects_bounded_stages(self):
        """The composition root must explicitly pass bounded stage classes."""
        from firecrawl_skill.research_store.orchestration.composition import (
            build_production_orchestrator,
        )

        source = inspect.getsource(build_production_orchestrator)
        assert "acquisition_stage_cls=BoundedAcquisitionStage" in source
        assert "extraction_stage_cls=ProductionBoundedExtractionStage" in source


class TestCommandDataclass:
    """AC-H: RunResearchCommand is a frozen (immutable) dataclass."""

    def test_run_research_command_is_frozen(self):
        from firecrawl_skill.research_store.orchestration.commands import (
            RunResearchCommand,
        )

        cmd = RunResearchCommand(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            spec={"query": "test"},
            search_plan={"queries": []},
        )
        with pytest.raises(FrozenInstanceError):
            cmd.run_id = UUID("00000000-0000-0000-0000-000000000002")  # type: ignore[misc]

    def test_run_research_command_fields(self):
        from firecrawl_skill.research_store.orchestration.commands import (
            RunResearchCommand,
        )

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
        from firecrawl_skill.research_store.orchestration.commands import (
            RunResearchCommand,
        )

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
        orch_dir = (
            SCRIPTS_DIR.parent
            / "src"
            / "firecrawl_skill"
            / "research_store"
            / "orchestration"
        )
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
        return (
            SCRIPTS_DIR.parent
            / "src"
            / "firecrawl_skill"
            / "research_store"
            / "smart_orchestrator.py"
        )

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


class TestResumeReaderNoSQL:
    """AC-K: PostgresResumeStateReader owns no SQL, cursors, or connections.

    The reader is a composition adapter over the canonical Phase-3
    repositories, reached only through the unit of work.  The allowed
    direction is::

        orchestration.resume -> ResumeStatePort -> PostgresResumeStateReader
          -> canonical repositories (via uow) -> SQL

    and the forbidden direction is ``reader -> uow.connection.cursor()``.
    These checks are AST-based, not source-string inspection.
    """

    READER = (
        SCRIPTS_DIR.parent
        / "src"
        / "firecrawl_skill"
        / "research_store"
        / "resume_state_repository.py"
    )
    EXPECTED_UOW_ROLES = frozenset(
        {
            "runs",
            "extraction_attempts",
            "snapshots",
            "strategy_revisions",
            "evidence_packets",
        }
    )

    @pytest.fixture(scope="class")
    @classmethod
    def reader_tree(cls):
        return ast.parse(cls.READER.read_text())

    def test_no_cursor_calls(self, reader_tree):
        """No .cursor() calls anywhere in the reader."""
        for node in ast.walk(reader_tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cursor"
            ):
                pytest.fail(f"line {node.lineno}: .cursor() call is forbidden")

    def test_no_connection_attribute(self, reader_tree):
        """No access to the raw uow.connection; go through repositories."""
        for node in ast.walk(reader_tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "connection"
                and isinstance(node.value, ast.Name)
                and node.value.id == "uow"
            ):
                pytest.fail(
                    f"line {node.lineno}: 'uow.connection' access is forbidden; "
                    "the reader must go through canonical repositories"
                )

    def test_no_execute_calls(self, reader_tree):
        """No .execute() calls; the reader only invokes repository methods."""
        for node in ast.walk(reader_tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
            ):
                pytest.fail(f"line {node.lineno}: .execute() call is forbidden")

    def test_composes_expected_uow_roles(self, reader_tree):
        """The reader composes exactly the expected canonical repository roles."""
        roles = {
            node.attr
            for node in ast.walk(reader_tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "uow"
        }
        missing = self.EXPECTED_UOW_ROLES - roles
        assert not missing, f"reader must compose uow roles; missing: {sorted(missing)}"


class TestResumeReaderIntegration:
    """AC-L: PostgresResumeStateReader live round-trip over the canonical repos.

    Skipped unless an explicit disposable PostgreSQL test DSN is configured.
    Seeds the minimal persisted shape a resumable run holds (run, candidate,
    extraction attempt, asset snapshot, document, ordered chunks, one
    acquisition->extraction transition), then asserts the reader's
    ``counts`` / ``completed_candidates`` / ``assets`` projections end to end.
    """

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )
    def test_reader_composes_canonical_repo_read(self):
        import psycopg

        from firecrawl_skill.research_store import postgres as pg
        from firecrawl_skill.research_store.resume_state_repository import (
            PostgresResumeStateReader,
        )

        database_url = TEST_DSN
        assert database_url is not None

        def sha(value: str) -> str:
            return sha256(value.encode()).hexdigest()

        run_id = uuid4()
        candidate_id = uuid4()
        attempt_id = uuid4()
        snapshot_id = uuid4()
        document_id = uuid4()
        chunk_a = uuid4()
        chunk_b = uuid4()
        source_id = uuid4()
        url = f"https://example.com/resume-reader/{run_id}"

        seed: list[tuple[LiteralString, tuple[str, ...]]] = [
            (
                (
                    "INSERT INTO research_runs (id, objective, query_plan, "
                    "skill_version, llm_model, state, execution_mode) "
                    "VALUES (%s, 'resume-reader', '{}', '1.0', 'm', 'created', "
                    "'agent_led')"
                ),
                (str(run_id),),
            ),
            (
                (
                    "INSERT INTO sources (id, canonical_url, source_type, "
                    "registered_domain) VALUES (%s, %s, 'web', 'example.com')"
                ),
                (str(source_id), url),
            ),
            (
                (
                    "INSERT INTO search_candidates (id, run_id, canonical_url, "
                    "canonical_url_sha256, original_url, domain, backend) "
                    "VALUES (%s, %s, %s, %s, %s, 'example.com', 'firecrawl')"
                ),
                (str(candidate_id), str(run_id), url, sha(url), url),
            ),
            (
                (
                    "INSERT INTO extraction_attempts (id, candidate_id, run_id, "
                    "method, method_version, start_time) "
                    "VALUES (%s, %s, %s, 'firecrawl_main_content', '1.0', now())"
                ),
                (str(attempt_id), str(candidate_id), str(run_id)),
            ),
            (
                (
                    "INSERT INTO asset_snapshots (id, source_id, requested_url, "
                    "final_url, retrieved_at, content_sha256, raw_blob_uri, "
                    "raw_byte_length, mime_type, firecrawl_version, crawl_options, "
                    "extraction_attempt_id) "
                    "VALUES (%s, %s, %s, %s, now(), %s, 'blob://x', 0, 'text/plain', "
                    "'0.0.0', '{}', %s)"
                ),
                (str(snapshot_id), str(source_id), url, url, sha("c"), str(attempt_id)),
            ),
            (
                (
                    "INSERT INTO documents (id, snapshot_id, title, parser_name, "
                    "parser_version, normalization_version, document_sha256, metadata) "
                    "VALUES (%s, %s, 'T', 'markdown-v1', '1.0', 'cleanup-v1', %s, '{}')"
                ),
                (str(document_id), str(snapshot_id), sha("d")),
            ),
            (
                (
                    "INSERT INTO chunks (id, document_id, ordinal, text, "
                    "content_sha256, token_count, chunker_name, chunker_version, "
                    "tokenizer_name) "
                    "VALUES (%s, %s, 0, 'a', %s, 1, 'structural-v1', '1.0', 'cl100k_base')"
                ),
                (str(chunk_a), str(document_id), sha("a")),
            ),
            (
                (
                    "INSERT INTO chunks (id, document_id, ordinal, text, "
                    "content_sha256, token_count, chunker_name, chunker_version, "
                    "tokenizer_name) "
                    "VALUES (%s, %s, 1, 'b', %s, 1, 'structural-v1', '1.0', 'cl100k_base')"
                ),
                (str(chunk_b), str(document_id), sha("b")),
            ),
            (
                (
                    "INSERT INTO research_run_transitions (run_id, "
                    "lifecycle_revision, prior_state, next_state, actor_type, "
                    "policy_version, idempotency_key) "
                    "VALUES (%s, 1, 'acquiring', 'extracting', 'system', 'p1', %s)"
                ),
                (str(run_id), uuid4().hex),
            ),
        ]
        with pg.connect(database_url) as conn, conn.cursor() as cur:
            for statement, params in seed:
                cur.execute(statement, params)
            conn.commit()

        try:
            reader = PostgresResumeStateReader(
                lambda: pg.PostgresUnitOfWork(database_url, "resume-reader-test")
            )

            counts = reader.counts(run_id)
            assert (counts.waves, counts.attempts, counts.assets) == (1, 1, 1), counts

            completed = reader.completed_candidates(run_id)
            assert completed == {str(candidate_id)}, completed

            assets = reader.assets(run_id)
            assert len(assets) == 1, assets
            asset = assets[0]
            assert asset["status"] == "complete"
            assert asset["resume_replay"] is True
            assert asset["extraction_attempt_id"] == str(attempt_id)
            assert asset["candidate_id"] == str(candidate_id)
            assert asset["snapshot_id"] == str(snapshot_id)
            assert asset["requested_url"] == url
            assert [str(c) for c in asset["chunk_ids"]] == [str(chunk_a), str(chunk_b)]
        finally:
            # research_run_transitions is append-only and blocks the run FK, so the
            # run and its transition are intentionally left in the disposable DB.
            deletes: list[tuple[LiteralString, tuple[str, ...]]] = [
                ("DELETE FROM chunks WHERE document_id=%s", (str(document_id),)),
                ("DELETE FROM documents WHERE id=%s", (str(document_id),)),
                (
                    "DELETE FROM asset_snapshots WHERE id=%s",
                    (str(snapshot_id),),
                ),
                (
                    "DELETE FROM extraction_attempts WHERE id=%s",
                    (str(attempt_id),),
                ),
                (
                    "DELETE FROM search_candidates WHERE id=%s",
                    (str(candidate_id),),
                ),
                ("DELETE FROM sources WHERE id=%s", (str(source_id),)),
            ]
            with pg.connect(database_url) as conn, conn.cursor() as cur:
                conn.autocommit = True
                for statement, params in deletes:
                    try:
                        cur.execute(statement, params)
                    except psycopg.Error as exc:  # best-effort; disposable test DB
                        print(f"cleanup skipped: {statement.split()[1]}: {exc}")


class TestPublicExports:
    """Verify the orchestration package exposes the expected public API."""

    def test_package_exports(self):
        import firecrawl_skill.research_store.orchestration as orch

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
        import firecrawl_skill.research_store.orchestration as orch

        for name in orch.__all__:
            assert hasattr(orch, name), f"Export '{name}' not found in module"
