from __future__ import annotations

import json
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import local_agent_assessment as base
import local_agent_pr_assessment as bootstrap


def profile():
    return base.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )


def bootstrap_runner(
    tmp_path: Path,
    *,
    source_head: str | None = None,
    control_sha: str | None = None,
):
    candidate = "a" * 40
    control = control_sha or "b" * 40
    merge_base = "c" * 40
    runner = bootstrap.ReviewedPRRunner.__new__(bootstrap.ReviewedPRRunner)
    cast(Any, runner).args = SimpleNamespace(
        sha=candidate,
        fetch=True,
        expected_ref=None,
    )
    runner.target_kind = "pr-head"
    runner.pr_number = 320
    runner.repo = tmp_path
    runner.control_root = tmp_path
    runner.profile = profile()
    runner.evidence = base.AssessmentEvidence(
        target_kind="pr-head",
        pr_number=320,
        requested_sha=candidate,
    )
    runner.candidate_test_base_sha = None
    runner.candidate_test_files = ()
    runner.candidate_test_blobs = {}
    runner.control_plane_source_sha = None
    cast(Any, runner)._journal = lambda stage: None
    cast(Any, runner)._fingerprint_control_plane = lambda: {"pr_bootstrap": "f" * 64}

    def fake_git(*args: str, check: bool = True):
        del check
        if args == ("remote", "get-url", "origin"):
            return SimpleNamespace(stdout=runner.profile.repository_remote + "\n")
        if args and args[0] == "fetch":
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=(source_head or candidate) + "\n")
        if args == ("rev-parse", "origin/main"):
            return SimpleNamespace(stdout=control + "\n")
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "FETCH_HEAD"):
            return SimpleNamespace(stdout=candidate + "\n")
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout=merge_base + "\n")
        if args and args[0] == "diff":
            return SimpleNamespace(stdout="")
        if args and args[0] == "cat-file":
            return SimpleNamespace(stdout="")
        if args and args[0] == "ls-tree":
            path = args[3]
            return SimpleNamespace(stdout=f"100644 blob {'d' * 40}\t{path}\n")
        if args and args[0] == "rev-parse" and ":" in args[1]:
            return SimpleNamespace(stdout="d" * 40 + "\n")
        raise AssertionError(f"unexpected git command: {args}")

    cast(Any, runner)._git = fake_git
    return runner, candidate, control


def test_pr_bootstrap_preflight_separates_reviewed_source_from_main_control(
    tmp_path: Path,
) -> None:
    runner, candidate, control = bootstrap_runner(tmp_path)

    runner.preflight(mutate=False)

    assert runner.control_plane_source_sha == candidate
    assert runner.evidence.control_sha == control
    assert runner.evidence.control_ref_start == control
    assert runner.evidence.pr_head_start == candidate
    assert runner.evidence.control_fingerprint == {"pr_bootstrap": "f" * 64}


def test_pr_bootstrap_preflight_rejects_nonexact_source_checkout(
    tmp_path: Path,
) -> None:
    runner, _candidate, _control = bootstrap_runner(
        tmp_path,
        source_head="e" * 40,
    )

    with pytest.raises(base.AssessmentError, match="bootstrap checkout is") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "STALE"


def test_pr_bootstrap_preflight_rejects_source_checkout_at_main(tmp_path: Path) -> None:
    runner, _candidate, _control = bootstrap_runner(
        tmp_path,
        control_sha="a" * 40,
    )

    with pytest.raises(base.AssessmentError, match="bootstrap is unnecessary") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "BLOCKED"


def test_pr_bootstrap_plan_records_both_trust_identities(tmp_path: Path) -> None:
    runner, candidate, control = bootstrap_runner(tmp_path)
    runner.assessment_id = "pr320-bootstrap"
    runner.profile_path = ROOT / "references/local-agent-assessment-profiles.toml"
    runner.evidence.profile_sha256 = base.sha256_file(runner.profile_path)
    runner.worktree = tmp_path / "worktree"
    runner.materials = tmp_path / "materials"
    runner.results = tmp_path / "results"

    plan = runner.plan()

    assert plan["requested_sha"] == candidate
    assert plan["control_plane_source_sha"] == candidate
    assert plan["control_snapshot_source_sha"] == control
    assert plan["control_sha"] == control


def test_pr_bootstrap_collects_trusted_membership_from_main_snapshot(
    tmp_path: Path,
) -> None:
    runner = bootstrap.ReviewedPRRunner.__new__(bootstrap.ReviewedPRRunner)
    group = base.PytestGroup(
        name="trusted",
        python_versions=("3.12",),
        selectors=("tests/unit/test_example.py",),
        expected_tests=1,
    )
    cast(Any, runner).profile = SimpleNamespace(
        pytest_groups=(group,),
        pr_test_python="3.12",
        pr_test_max_nodes=8,
    )
    runner.control_snapshot = tmp_path / "control-main"
    runner.control_snapshot.mkdir()
    runner.worktree = tmp_path / "candidate"
    runner.worktree.mkdir()
    runner.candidate_test_files = ()
    runner.candidate_test_base_sha = "b" * 40
    runner.evidence = base.AssessmentEvidence()
    runner.control_snapshot_inventory = None
    observed_cwds: list[Path] = []

    def collect(
        name,
        python,
        selectors,
        *,
        cwd,
        env,
        max_nodes,
        failure_status,
        reject_filtered_collection=False,
        blocked_test_module_plugins=(),
    ):
        del name, python, selectors
        del env, max_nodes, failure_status
        del reject_filtered_collection, blocked_test_module_plugins
        observed_cwds.append(cwd)
        return ("tests/unit/test_example.py::test_one",)

    cast(Any, runner)._collect_pytest_nodes = collect
    cast(Any, runner)._run_exact_pytest_nodes = lambda *args, **kwargs: None

    runner._run_pr_pytest(
        {"3.12": tmp_path / "venv"},
        {},
    )

    assert observed_cwds == [runner.control_snapshot]
    assert runner.control_snapshot_inventory == {}


def test_pr_bootstrap_control_snapshot_is_exact_git_archive(tmp_path: Path) -> None:
    runner = bootstrap.ReviewedPRRunner.__new__(bootstrap.ReviewedPRRunner)
    runner.materials = tmp_path / "materials"
    runner.materials.mkdir()
    runner.control_snapshot = runner.materials / "control-main"
    runner.evidence = base.AssessmentEvidence(control_sha="b" * 40)
    cast(Any, runner)._journal = lambda stage: None

    payload = b"trusted main\n"

    def fake_git(*args: str, check: bool = True):
        del check
        assert args[0] == "archive"
        output = next(item for item in args if item.startswith("--output="))
        archive = Path(output.split("=", 1)[1])
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo("tests/unit/test_control.py")
            info.size = len(payload)
            bundle.addfile(info, BytesIO(payload))
        return SimpleNamespace(stdout="")

    cast(Any, runner)._git = fake_git
    runner._create_control_snapshot()

    assert (
        runner.control_snapshot / "tests/unit/test_control.py"
    ).read_bytes() == payload
    assert not (runner.materials / "control-main.tar").exists()


def test_pr_bootstrap_fingerprint_extends_base_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = bootstrap.ReviewedPRRunner.__new__(bootstrap.ReviewedPRRunner)
    monkeypatch.setattr(
        bootstrap.BaseRunner,
        "_fingerprint_control_plane",
        lambda _self: {"runner": "a" * 64},
    )

    fingerprints = runner._fingerprint_control_plane()

    assert fingerprints["runner"] == "a" * 64
    assert fingerprints["pr_bootstrap"] == base.sha256_file(
        SCRIPTS / "local_agent_pr_assessment.py"
    )


def test_shim_routes_pr_mode_through_dispatch_module() -> None:
    shim = (SCRIPTS / "local-agent-assessment").read_text(encoding="utf-8")

    assert "local_agent_pr_assessment" in shim
    assert '"--pr"' in shim
    assert '"pr-head"' in shim


def test_pr_pytest_entrypoint_is_isolated_without_changed_test_modules() -> None:
    python = Path("/trusted/venv/bin/python")

    argv = bootstrap._pr_pytest_entry_argv(python)

    assert argv[:4] == [
        str(python),
        "-P",
        "-c",
        base.CANDIDATE_PYTEST_LAUNCHER,
    ]
    assert argv[4] == "[]"
    assert argv != [str(python), "-m", "pytest"]


def test_pr_candidate_discovery_rejects_symlink_git_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    path = "tests/unit/test_symlink.py"
    runner = bootstrap.BaseRunner.__new__(bootstrap.BaseRunner)
    cast(Any, runner).args = SimpleNamespace(sha=candidate)
    monkeypatch.setattr(
        bootstrap,
        "BaseDiscoverCandidateTestFiles",
        lambda _runner, _control_sha: (path,),
    )
    cast(Any, runner)._git = lambda *args, **_kwargs: SimpleNamespace(
        stdout=f"120000 blob {'b' * 40}\t{path}\n"
    )

    with pytest.raises(base.AssessmentError, match="regular Git file") as exc:
        bootstrap._pr_discover_candidate_test_files(runner, "c" * 40)

    assert exc.value.status == "BLOCKED"


def test_pr_candidate_discovery_accepts_regular_git_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    path = "tests/unit/test_regular.py"
    runner = bootstrap.BaseRunner.__new__(bootstrap.BaseRunner)
    cast(Any, runner).args = SimpleNamespace(sha=candidate)
    monkeypatch.setattr(
        bootstrap,
        "BaseDiscoverCandidateTestFiles",
        lambda _runner, _control_sha: (path,),
    )
    cast(Any, runner)._git = lambda *args, **_kwargs: SimpleNamespace(
        stdout=f"100644 blob {'b' * 40}\t{path}\n"
    )

    assert bootstrap._pr_discover_candidate_test_files(runner, "c" * 40) == (path,)
    assert runner.candidate_test_blobs == {path: "b" * 40}


def test_pr_candidate_source_manifest_is_bound_to_exact_git_blobs(
    tmp_path: Path,
) -> None:
    runner = bootstrap.BaseRunner.__new__(bootstrap.BaseRunner)
    relative = "tests/unit/test_candidate.py"
    cast(Any, runner).candidate_test_files = (relative,)
    cast(Any, runner).candidate_test_blobs = {relative: "b" * 40}
    cast(Any, runner).args = SimpleNamespace(sha="a" * 40)
    cast(Any, runner).materials = tmp_path
    source = "def test_candidate():\n    assert True\n"

    def fake_git(*args: str, **_kwargs):
        assert args == ("cat-file", "-p", "b" * 40)
        return SimpleNamespace(stdout=source)

    cast(Any, runner)._git = fake_git

    authority = bootstrap._prepare_pr_candidate_test_source_manifest(runner)

    assert authority is not None
    path, sha256 = authority
    raw = path.read_bytes()
    assert base.sha256_bytes(raw) == sha256
    payload = json.loads(raw)
    assert payload["candidate_sha"] == "a" * 40
    assert payload["entries"] == [
        {"path": relative, "blob_sha": "b" * 40, "source": source}
    ]


def test_pr_candidate_worktree_rejects_symlinked_test_path(tmp_path: Path) -> None:
    worktree = tmp_path / "candidate"
    test_dir = worktree / "tests/unit"
    test_dir.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("def test_outside(): pass\n", encoding="utf-8")
    relative = "tests/unit/test_escape.py"
    (worktree / relative).symlink_to(outside)

    runner = bootstrap.BaseRunner.__new__(bootstrap.BaseRunner)
    cast(Any, runner).worktree = worktree
    cast(Any, runner).candidate_test_files = (relative,)

    with pytest.raises(base.AssessmentError, match="became symlinked") as exc:
        bootstrap._validate_pr_candidate_test_worktree_paths(runner)

    assert exc.value.status == "STALE"


def test_pr_candidate_collection_requires_node_from_every_changed_module(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "candidate"
    first = "tests/unit/test_first.py"
    second = "tests/unit/test_second.py"
    for relative in (first, second):
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_one(): pass\n", encoding="utf-8")

    runner = bootstrap.BaseRunner.__new__(bootstrap.BaseRunner)
    cast(Any, runner).worktree = worktree
    cast(Any, runner).candidate_test_files = (first, second)
    monkeypatch.setattr(
        bootstrap,
        "BaseCollectPytestNodes",
        lambda *_args, **_kwargs: (f"{first}::test_one",),
    )

    with pytest.raises(
        base.AssessmentError, match="omitted candidate test modules"
    ) as exc:
        bootstrap._pr_collect_pytest_nodes(
            runner,
            "candidate-regressions",
            Path("/trusted/venv/bin/python"),
            (first, second),
            cwd=worktree,
            env={},
            max_nodes=8,
            failure_status="FAIL",
            reject_filtered_collection=True,
            blocked_test_module_plugins=(first, second),
        )

    assert exc.value.status == "FAIL"
    assert second in str(exc.value)


@pytest.mark.parametrize(
    ("source_head", "local_main", "expected"),
    [
        ("a" * 40, "b" * 40, True),
        ("a" * 40, "a" * 40, False),
        ("c" * 40, "b" * 40, False),
    ],
)
def test_pr_dispatch_bootstrap_probe_requires_candidate_head_distinct_from_main(
    monkeypatch: pytest.MonkeyPatch,
    source_head: str,
    local_main: str,
    expected: bool,
) -> None:
    requested = "a" * 40

    def fake_run(argv, **kwargs):
        assert argv[-3:] == ["rev-parse", "HEAD", "origin/main"]
        assert kwargs["check"] is False
        return SimpleNamespace(
            returncode=0,
            stdout=f"{source_head}\n{local_main}\n",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    assert (
        bootstrap._source_checkout_requires_bootstrap(
            ["run", "--sha", requested],
        )
        is expected
    )


def test_pr_dispatch_uses_base_runner_for_trusted_main_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_runner = base.Runner
    original_pytest_entry_argv = base.pytest_entry_argv
    original_discover = bootstrap.BaseRunner._discover_candidate_test_files
    original_collect = bootstrap.BaseRunner._collect_pytest_nodes
    original_run_exact = bootstrap.BaseRunner._run_exact_pytest_nodes
    argv = ["run", "--sha", "a" * 40]
    observed: list[tuple[list[str] | None, type]] = []

    monkeypatch.setattr(
        bootstrap,
        "_source_checkout_requires_bootstrap",
        lambda _argv=None: False,
    )

    def fake_main(passed_argv=None):
        assert base.pytest_entry_argv is bootstrap._pr_pytest_entry_argv
        assert (
            bootstrap.BaseRunner._discover_candidate_test_files
            is bootstrap._pr_discover_candidate_test_files
        )
        assert (
            bootstrap.BaseRunner._collect_pytest_nodes
            is bootstrap._pr_collect_pytest_nodes
        )
        assert (
            bootstrap.BaseRunner._run_exact_pytest_nodes
            is bootstrap._pr_run_exact_pytest_nodes
        )
        observed.append((passed_argv, base.Runner))
        return 17

    monkeypatch.setattr(base, "main", fake_main)

    assert bootstrap.main(argv) == 17
    assert observed == [(argv, original_runner)]
    assert base.Runner is original_runner
    assert base.pytest_entry_argv is original_pytest_entry_argv
    assert bootstrap.BaseRunner._discover_candidate_test_files is original_discover
    assert bootstrap.BaseRunner._collect_pytest_nodes is original_collect
    assert bootstrap.BaseRunner._run_exact_pytest_nodes is original_run_exact


def test_pr_dispatch_temporarily_uses_bootstrap_for_self_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_runner = base.Runner
    original_pytest_entry_argv = base.pytest_entry_argv
    argv = ["run", "--sha", "a" * 40]

    monkeypatch.setattr(
        bootstrap,
        "_source_checkout_requires_bootstrap",
        lambda _argv=None: True,
    )

    def fake_main(passed_argv=None):
        assert passed_argv == argv
        assert base.Runner is bootstrap.ReviewedPRRunner
        assert base.pytest_entry_argv is bootstrap._pr_pytest_entry_argv
        return 23

    monkeypatch.setattr(base, "main", fake_main)

    assert bootstrap.main(argv) == 23
    assert base.Runner is original_runner
    assert base.pytest_entry_argv is original_pytest_entry_argv


def test_pr_dispatch_restores_control_hooks_after_bootstrap_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_runner = base.Runner
    original_pytest_entry_argv = base.pytest_entry_argv
    original_discover = bootstrap.BaseRunner._discover_candidate_test_files
    original_collect = bootstrap.BaseRunner._collect_pytest_nodes
    original_run_exact = bootstrap.BaseRunner._run_exact_pytest_nodes

    monkeypatch.setattr(
        bootstrap,
        "_source_checkout_requires_bootstrap",
        lambda _argv=None: True,
    )

    def fail_main(_argv=None):
        assert base.Runner is bootstrap.ReviewedPRRunner
        assert base.pytest_entry_argv is bootstrap._pr_pytest_entry_argv
        assert (
            bootstrap.BaseRunner._discover_candidate_test_files
            is bootstrap._pr_discover_candidate_test_files
        )
        assert (
            bootstrap.BaseRunner._collect_pytest_nodes
            is bootstrap._pr_collect_pytest_nodes
        )
        assert (
            bootstrap.BaseRunner._run_exact_pytest_nodes
            is bootstrap._pr_run_exact_pytest_nodes
        )
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(base, "main", fail_main)

    with pytest.raises(RuntimeError, match="injected bootstrap failure"):
        bootstrap.main(["run", "--sha", "a" * 40])

    assert base.Runner is original_runner
    assert base.pytest_entry_argv is original_pytest_entry_argv
    assert bootstrap.BaseRunner._discover_candidate_test_files is original_discover
    assert bootstrap.BaseRunner._collect_pytest_nodes is original_collect
    assert bootstrap.BaseRunner._run_exact_pytest_nodes is original_run_exact


def test_pr_dispatch_documentation_distinguishes_steady_state_and_bootstrap() -> None:
    content = (ROOT / "references/local-agent-assessment.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(content.split())

    assert "Steady-state PR assessment is main-owned." in normalized
    assert "Pre-merge self-assessment is the only bootstrap exception." in normalized
    assert "temporarily substitute `ReviewedPRRunner`" in normalized
    assert (
        "trusted isolated pytest launcher for every PR-mode pytest process"
        in normalized
    )
    assert "control hooks are restored in a `finally` block" in normalized
    assert "regular Git blob at the exact candidate SHA" in normalized
    assert "regular file contained by the detached candidate worktree" in normalized
    assert (
        "at least one accepted node for every discovered changed candidate test module"
        in normalized
    )
