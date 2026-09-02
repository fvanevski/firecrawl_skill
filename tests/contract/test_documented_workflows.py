"""Exercise the canonical RC-9 lifecycle and transaction documentation."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from firecrawl_skill.research_store.invocation_service import InvocationService
from firecrawl_skill.research_store.run_service import ResearchRunService

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SKILL_ROOT = SCRIPTS.parent
RUN_ID = "fr_" + "a" * 32
INVOCATION_ID = "fc_" + "b" * 32
ASSESSMENT_PROFILE = SKILL_ROOT / "references/local-agent-assessment-profiles.toml"
ASSESSMENT_SHIM = SCRIPTS / "local-agent-assessment"
CI_TRANSITION = SKILL_ROOT / "ci/merge-policy-transition.toml"


def _ci_transition_state() -> str:
    return str(
        tomllib.loads(CI_TRANSITION.read_text(encoding="utf-8"))["transition_state"]
    )


DRAIN_DOCUMENTS = (
    "README.md",
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


def test_local_assessment_entrypoint_and_trusted_inputs_are_present() -> None:
    assert ASSESSMENT_SHIM.is_file()
    assert os.access(ASSESSMENT_SHIM, os.X_OK)
    assert (SCRIPTS / "local_agent_assessment.py").is_file()
    assert (SKILL_ROOT / "tests/unit/test_local_agent_assessment.py").is_file()
    assert (SKILL_ROOT / "requirements-ci.txt").is_file()
    legacy_locks = (
        SKILL_ROOT / "requirements-local-agent-assessment-py311.lock",
        SKILL_ROOT / "requirements-local-agent-assessment-py312.lock",
    )
    if _ci_transition_state() == "pending-exact-head-proof":
        assert all(path.exists() for path in legacy_locks)
    else:
        assert not any(path.exists() for path in legacy_locks)
    assert (SKILL_ROOT / "references/local-agent-assessment.md").is_file()


def test_phase1_assessment_profile_selectors_are_real() -> None:
    document = tomllib.loads(ASSESSMENT_PROFILE.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    profile = document["profiles"]["phase1-control-policy"]
    assert profile["expected_skips"] == 0
    assert profile["requires_disposable_services"] is True
    assert profile["candidate_code_trust"] == "trusted-ref-only"
    assert profile["trusted_refs"] == ["origin/main"]
    assert profile["allow_reviewed_pr_head"] is True
    assert profile["pr_test_python"] == "3.12"
    assert profile["pr_test_roots"] == [
        "tests/unit",
        "tests/integration",
        "tests/contract",
        "tests/acceptance",
    ]
    assert profile["pr_test_max_files"] == 64
    assert profile["pr_test_max_nodes"] == 512

    for group in profile["pytest_groups"]:
        for selector in group["selectors"]:
            path_text, _, node_text = selector.partition("::")
            path = SKILL_ROOT / path_text
            assert path.is_file(), selector
            if node_text:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                functions = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                assert node_text.split("::", 1)[0] in functions, selector


def test_local_assessment_locks_are_hashed_and_pin_tools() -> None:
    content = (SKILL_ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    assert "pytest==9.1.1" in content
    assert "pyrefly==1.2.0" in content
    assert "ruff==0.16.5" in content
    runner = (SCRIPTS / "local_agent_assessment.py").read_text(encoding="utf-8")
    assert '"toolchain_manifest": self.control_root / "requirements-ci.txt"' in runner
    assert "requirements-local-agent-assessment-py" not in runner


def test_local_assessment_documentation_preserves_authority_boundary() -> None:
    content = (SKILL_ROOT / "references/local-agent-assessment.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(content.split())
    assert "HOST_EVIDENCE_RESULT" in content
    assert "GATE_DECISION=NOT_EVALUATED" in content
    assert "candidate worktree never supplies" in normalized
    assert "`trusted-ref`" in content
    assert "`pr-head`" in content
    assert "refs/pull/<PR_NUMBER>/head" in content
    assert "hostile/untrusted or arbitrary fork code" in content
    assert "candidate_test_manifest" in content
    assert "local-agent-assessment.mjs" in content
    assert "--spec /tmp/opencode/verify/assessments/<assessment-id>.json" in content
    assert '"execution": "repository-owned"' in content
    assert '"authority": "base"' in content
    assert '"head_ref": "refs/pull/<PR_NUMBER>/head"' in content
    assert '"--target-kind", "pr-head"' in content
    assert '"--pr", "{pr_number}"' in content
    assert '"--sha", "{head_sha}"' in content
    assert content.count('"--workspace-root", "{workspace_root}"') == 2
    assert content.count("--workspace-root <gateway-supplied-workspace-root>") == 2
    assert (
        "/tmp/opencode/verify/repository-owned/<assessment-id>/results/"
        "<assessment-id>/assessment.json"
        in content
    )
    assert "Do not use the retired public" in content
    assert "ISOLATION_BREACH" in content
    assert "entire process group" in content
    assert "before creating recovery HOME/TMP/XDG/material state" in content
    assert "Do not reuse the earlier Gate #312 assessment" in content
    assert "pytest_plugins" in content
    assert "collection-time" in content
    assert "reported collected count" in content
    assert "every auto-loaded `conftest.py` ancestor" in content
    assert "exact current-main Git tree state" in content
    assert "compiles every changed module before `pytest.main()`" in normalized
    assert "at most one changed candidate test module" in normalized
    assert "fresh pytest process" in normalized
    assert "Python with `-P`" in content
    assert (
        "trusted isolated pytest launcher for every PR-mode pytest process"
        in normalized
    )
    assert "every repository path referenced by a trusted profile selector" in content
    assert "caller's candidate collection status" in content


def test_local_assessment_pr_authority_hardening_is_wired() -> None:
    source = (SCRIPTS / "local_agent_assessment.py").read_text(encoding="utf-8")

    assert "CANDIDATE_PYTEST_LAUNCHER" in source
    assert "pr_pytest_conftest_paths" in source
    assert '"-P"' in source
    assert '"--no-renames"' in source
    assert '"--diff-filter=AMD"' in source
    assert "blocked_test_module_plugins=self.candidate_test_files" in source
    assert "candidate cannot replace trusted regression implementation" in source
    assert "failure_status=failure_status" in source
    assert "_require_matching_optional_regular_path" in source
    assert "CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV" in source
    assert "_collect_candidate_pytest_nodes_isolated" in source
    assert "_run_candidate_pytest_nodes_isolated" in source
    assert "cannot select multiple changed test modules" in source


@pytest.mark.parametrize("rel_path", DRAIN_DOCUMENTS)
def test_runtime_workflows_use_canonical_drain_helper(rel_path: str) -> None:
    content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
    assert "drain_index_jobs.py" in content


def test_canonical_workflow_uses_controller_parser_and_projection_order() -> None:
    from firecrawl_skill.research_store.research_controller_cli import build_parser

    parser = build_parser()
    documented_commands = (
        ["run", "Research objective"],
        ["run", "--delivery-mode", "host_handoff", "Research objective"],
        ["run", "--delivery-mode", "self_synthesized", "Research objective"],
        ["run", "--retained-only", "Research objective"],
        ["run", "--curated", "Research objective"],
        ["continue", RUN_ID],
        ["status", RUN_ID],
        ["result", RUN_ID],
        ["action", "oa_" + "c" * 32],
    )
    for argv in documented_commands:
        assert parser.parse_args(argv).command == argv[0]

    content = (SKILL_ROOT / "references/authoritative-workflows.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/fresearch run" in content
    assert "scripts/fresearch continue" in content
    assert "scripts/fresearch result" in content
    assert "Do not insert low-level lifecycle operations" in content

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

    from firecrawl_skill.research_store.corpus_service import CorpusService

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
    from firecrawl_skill.research_store.retrieval.projection.indexing import IndexWorker

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
    from firecrawl_skill.research_store.workflow_service import (
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
    from firecrawl_skill.research_store.corpus_service import CorpusService
    from firecrawl_skill.research_store.domain import IngestRequest

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
