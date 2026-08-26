from __future__ import annotations

import importlib.util
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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


def bootstrap_runner(tmp_path: Path, *, source_head: str | None = None):
    candidate = "a" * 40
    control = "b" * 40
    merge_base = "c" * 40
    runner = bootstrap.ReviewedPRRunner.__new__(bootstrap.ReviewedPRRunner)
    runner.args = SimpleNamespace(
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
    runner.control_plane_source_sha = None
    runner._journal = lambda _stage: None
    runner._fingerprint_control_plane = lambda: {"pr_bootstrap": "f" * 64}

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
        if args and args[0] == "rev-parse" and ":" in args[1]:
            return SimpleNamespace(stdout="d" * 40 + "\n")
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git
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
    runner.profile = SimpleNamespace(
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
        _name,
        _python,
        _selectors,
        *,
        cwd,
        env,
        max_nodes,
        failure_status,
        reject_filtered_collection=False,
        blocked_test_module_plugins=(),
    ):
        del (
            env,
            max_nodes,
            failure_status,
            reject_filtered_collection,
            blocked_test_module_plugins,
        )
        observed_cwds.append(cwd)
        return ("tests/unit/test_example.py::test_one",)

    runner._collect_pytest_nodes = collect
    runner._run_exact_pytest_nodes = lambda *args, **kwargs: None

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
    runner._journal = lambda _stage: None

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

    runner._git = fake_git
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


def test_shim_dispatches_pr_mode_through_reviewed_bootstrap() -> None:
    shim = (SCRIPTS / "local-agent-assessment").read_text(encoding="utf-8")

    assert "local_agent_pr_assessment" in shim
    assert '"--pr"' in shim
    assert '"pr-head"' in shim
