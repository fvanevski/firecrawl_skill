from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def assessment_module():
    loader = SourceFileLoader(
        "local_agent_assessment_under_test",
        str(ROOT / "scripts/local_agent_assessment.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def service_payload() -> str:
    return json.dumps(
        {
            "schema_version": "firecrawl-disposable-services-v1",
            "namespace": "gate312-test",
            "host": "127.0.0.1",
            "images": {
                "postgres": "postgres:16-alpine@sha256:" + "a" * 64,
                "qdrant": "qdrant/qdrant:v1.18.3-unprivileged@sha256:" + "b" * 64,
            },
            "postgres": {
                "container": "gate312-test_pg",
                "port": 55436,
                "database": "gate312_test_test",
            },
            "qdrant": {
                "container": "gate312-test_qdrant",
                "port": 55437,
                "ready_url": "http://127.0.0.1:55437/readyz",
            },
            "environment": {
                "RESEARCH_STORE_TEST_DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:55436/gate312_test_test",
                "RESEARCH_STORE_TEST_ALLOW_RESET": "gate312_test_test",
                "QDRANT_URL": "http://127.0.0.1:55437",
                "RESEARCH_STORE_TEST_QDRANT_URL": "http://127.0.0.1:55437",
                "RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET": "http://127.0.0.1:55437",
            },
        }
    )


def test_profile_is_declarative_and_preserves_exact_gate_groups() -> None:
    module = assessment_module()
    profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )

    assert profile.python_versions == ("3.11", "3.12")
    assert profile.static_python == "3.12"
    assert profile.candidate_code_trust == "trusted-ref-only"
    assert profile.trusted_refs == ("origin/main",)
    assert (
        profile.repository_remote == "https://github.com/fvanevski/firecrawl_skill.git"
    )
    assert profile.requires_fresh_fetch is True
    assert profile.allow_reviewed_pr_head is True
    assert profile.pr_test_python == "3.12"
    assert profile.pr_test_roots == (
        "tests/unit",
        "tests/integration",
        "tests/contract",
        "tests/acceptance",
    )
    assert profile.pr_test_max_files == 64
    assert profile.pr_test_max_nodes == 512
    assert [group.name for group in profile.pytest_groups] == [
        "controller",
        "deterministic-policy",
        "temporal",
        "acquisition-orchestration",
        "falsification-controller",
        "falsification-policy",
        "falsification-replay",
    ]
    assert sum(len(group.selectors) for group in profile.pytest_groups) == 33
    assert sum(group.expected_tests for group in profile.pytest_groups) == 338


def pr_preflight_runner(
    module,
    tmp_path: Path,
    *,
    pr_head: str,
    policy_match: bool = True,
    pytest_control_match: bool = True,
    trusted_test_match: bool = True,
    protected_conftest_state: str = "match",
):
    candidate_sha = "a" * 40
    control_sha = "b" * 40
    merge_base = "c" * 40
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(
        sha=candidate_sha,
        fetch=True,
        expected_ref=None,
    )
    runner.target_kind = "pr-head"
    runner.pr_number = 320
    runner.repo = tmp_path
    runner.control_root = tmp_path
    runner.profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )
    runner.evidence = module.AssessmentEvidence(
        target_kind="pr-head",
        pr_number=320,
        requested_sha=candidate_sha,
    )
    runner.candidate_test_base_sha = None
    runner.candidate_test_files = ()
    runner._fingerprint_control_plane = dict
    runner._journal = lambda _stage: None
    trusted_test_path = runner.profile.pytest_groups[0].selectors[0].split("::", 1)[0]

    def fake_git(*args: str, check: bool = True):
        del check
        if args == ("remote", "get-url", "origin"):
            return SimpleNamespace(stdout=runner.profile.repository_remote + "\n")
        if args and args[0] == "fetch":
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "origin/main") or args == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=control_sha + "\n")
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "FETCH_HEAD"):
            return SimpleNamespace(stdout=pr_head + "\n")
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout=merge_base + "\n")
        if args and args[0] == "diff":
            return SimpleNamespace(stdout="")
        if args and args[0] == "cat-file":
            return SimpleNamespace(stdout="")
        if args and args[0] == "ls-tree":
            commit, path = args[1], args[3]
            blob = "d" * 40
            if commit == candidate_sha:
                if not pytest_control_match and path == module.PR_TEST_CONTROL_PATHS[0]:
                    blob = "e" * 40
                if path == "tests/conftest.py":
                    if protected_conftest_state == "missing":
                        return SimpleNamespace(stdout="")
                    if protected_conftest_state == "different":
                        blob = "e" * 40
            return SimpleNamespace(stdout=f"100644 blob {blob}\t{path}\n")
        if args and args[0] == "rev-parse" and ":" in args[1]:
            commit, path = args[1].split(":", 1)
            blob = "d" * 40
            if commit == candidate_sha and (
                (not policy_match and path == "pyproject.toml")
                or (
                    not pytest_control_match and path == module.PR_TEST_CONTROL_PATHS[0]
                )
                or (not trusted_test_match and path == trusted_test_path)
            ):
                blob = "e" * 40
            return SimpleNamespace(stdout=blob + "\n")
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git
    return runner


def test_target_contract_rejects_unauthorized_pr_and_ref_combinations() -> None:
    module = assessment_module()
    profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )

    assert module.validate_target_args(
        SimpleNamespace(
            target_kind="trusted-ref",
            pr=None,
            expected_ref="origin/main",
            fetch=True,
        ),
        profile,
    ) == ("trusted-ref", None)
    assert module.validate_target_args(
        SimpleNamespace(
            target_kind="pr-head",
            pr=320,
            expected_ref=None,
            fetch=True,
        ),
        profile,
    ) == ("pr-head", 320)

    with pytest.raises(module.AssessmentError, match="does not accept --pr"):
        module.validate_target_args(
            SimpleNamespace(
                target_kind="trusted-ref",
                pr=320,
                expected_ref="origin/main",
                fetch=True,
            ),
            profile,
        )
    with pytest.raises(module.AssessmentError, match="does not accept --expected-ref"):
        module.validate_target_args(
            SimpleNamespace(
                target_kind="pr-head",
                pr=320,
                expected_ref="origin/main",
                fetch=True,
            ),
            profile,
        )
    with pytest.raises(module.AssessmentError, match="requires --fetch"):
        module.validate_target_args(
            SimpleNamespace(
                target_kind="pr-head",
                pr=320,
                expected_ref=None,
                fetch=False,
            ),
            profile,
        )


def test_pr_head_preflight_rejects_wrong_requested_sha(tmp_path: Path) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(module, tmp_path, pr_head="f" * 40)

    with pytest.raises(module.AssessmentError, match="not requested SHA") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "STALE"
    assert runner.evidence.pr_head_start == "f" * 40


def test_pr_head_preflight_rejects_candidate_static_policy_substitution(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(
        module,
        tmp_path,
        pr_head="a" * 40,
        policy_match=False,
    )

    with pytest.raises(module.AssessmentError, match="cannot replace trusted") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "BLOCKED"


def test_pr_head_preflight_rejects_candidate_pytest_control_substitution(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(
        module,
        tmp_path,
        pr_head="a" * 40,
        pytest_control_match=False,
    )

    with pytest.raises(module.AssessmentError, match="trusted pytest control") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "BLOCKED"


@pytest.mark.parametrize("state", ["missing", "different"])
def test_pr_head_preflight_rejects_current_main_ancestor_conftest_drift(
    tmp_path: Path, state: str
) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(
        module,
        tmp_path,
        pr_head="a" * 40,
        protected_conftest_state=state,
    )

    with pytest.raises(module.AssessmentError, match="trusted pytest control") as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "BLOCKED"
    assert "tests/conftest.py" in str(exc.value)


def test_pr_head_preflight_accepts_exact_current_main_ancestor_conftests(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(module, tmp_path, pr_head="a" * 40)

    runner.preflight(mutate=False)

    assert runner.evidence.control_sha == "b" * 40


def test_pr_head_preflight_rejects_trusted_regression_substitution(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    runner = pr_preflight_runner(
        module,
        tmp_path,
        pr_head="a" * 40,
        trusted_test_match=False,
    )

    with pytest.raises(
        module.AssessmentError, match="trusted regression implementation"
    ) as exc:
        runner.preflight(mutate=False)

    assert exc.value.status == "BLOCKED"


def test_pr_pytest_conftest_paths_include_every_auto_loaded_ancestor() -> None:
    module = assessment_module()
    profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )

    assert module.pr_pytest_conftest_paths(profile.pr_test_roots) == (
        "conftest.py",
        "tests/acceptance/conftest.py",
        "tests/conftest.py",
        "tests/contract/conftest.py",
        "tests/integration/conftest.py",
        "tests/unit/conftest.py",
    )


def test_candidate_test_discovery_rejects_changed_nested_conftest() -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha="a" * 40)
    runner.profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )
    runner.candidate_test_base_sha = None

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout="c" * 40 + "\n")
        if args[:5] == (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AMD",
        ):
            return SimpleNamespace(stdout="tests/unit/conftest.py\0")
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git

    with pytest.raises(module.AssessmentError, match="pytest control files") as exc:
        runner._discover_candidate_test_files("b" * 40)

    assert exc.value.status == "BLOCKED"


def test_candidate_test_discovery_rejects_added_parent_conftest() -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha="a" * 40)
    runner.profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )
    runner.candidate_test_base_sha = None

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout="c" * 40 + "\n")
        if args[:5] == (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AMD",
        ):
            assert "tests/conftest.py" in args
            return SimpleNamespace(stdout="tests/conftest.py\0")
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git

    with pytest.raises(module.AssessmentError, match="pytest control files") as exc:
        runner._discover_candidate_test_files("b" * 40)

    assert exc.value.status == "BLOCKED"


def test_candidate_test_discovery_rejects_declared_pytest_plugins() -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha="a" * 40)
    runner.profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )
    runner.candidate_test_base_sha = None

    source = (
        'pytest_plugins = ("candidate_hook",)\n\n\ndef test_candidate():\n    pass\n'
    )

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout="c" * 40 + "\n")
        if args[:5] == (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AMD",
        ):
            return SimpleNamespace(stdout="")
        if args[:4] == ("diff", "--name-only", "-z", "--diff-filter=AMR"):
            return SimpleNamespace(stdout="tests/unit/test_candidate_plugin.py\0")
        if args and args[0] == "show":
            return SimpleNamespace(stdout=source)
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git

    with pytest.raises(
        module.AssessmentError, match="cannot declare pytest_plugins"
    ) as exc:
        runner._discover_candidate_test_files("b" * 40)

    assert exc.value.status == "BLOCKED"
    assert "pytest_plugins" in exc.value.args[0]


def test_candidate_test_discovery_excludes_trusted_profile_paths() -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha="a" * 40)
    runner.profile = module.load_profile(
        ROOT / "references/local-agent-assessment-profiles.toml",
        "phase1-control-policy",
    )
    runner.candidate_test_base_sha = None

    trusted = runner.profile.pytest_groups[0].selectors[0].split("::", 1)[0]
    candidate = "tests/unit/test_candidate_owned.py"
    shown: list[str] = []

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "merge-base":
            return SimpleNamespace(stdout="c" * 40 + "\n")
        if args[:5] == (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AMD",
        ):
            return SimpleNamespace(stdout="")
        if args[:4] == ("diff", "--name-only", "-z", "--diff-filter=AMR"):
            return SimpleNamespace(stdout=f"{trusted}\0{candidate}\0")
        if args and args[0] == "show":
            shown.append(args[1])
            return SimpleNamespace(stdout="def test_candidate():\n    pass\n")
        raise AssertionError(f"unexpected git command: {args}")

    runner._git = fake_git

    assert runner._discover_candidate_test_files("b" * 40) == (candidate,)
    assert shown == [f"{'a' * 40}:{candidate}"]
    assert runner.candidate_test_base_sha == "c" * 40


def test_pytest_entry_argv_preserves_trusted_path_and_hardens_candidate_path() -> None:
    module = assessment_module()
    python = Path("/tmp/venv/bin/python")

    assert module.pytest_entry_argv(python) == [str(python), "-m", "pytest"]

    guarded = module.pytest_entry_argv(
        python,
        ("tests/unit/test_b.py", "tests/unit/test_a.py"),
    )
    assert guarded[:4] == [str(python), "-P", "-c", module.CANDIDATE_PYTEST_LAUNCHER]
    assert json.loads(guarded[4]) == [
        "tests/unit/test_a.py",
        "tests/unit/test_b.py",
    ]


def test_candidate_pytest_launcher_blocks_dynamic_plugins_and_import_shadow(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    (tmp_path / "pytest.py").write_text(
        "raise RuntimeError('candidate pytest shadow loaded')\n",
        encoding="utf-8",
    )
    (tmp_path / "candidate_filter.py").write_text(
        "def pytest_collection_modifyitems(items):\n    del items[1:]\n",
        encoding="utf-8",
    )
    (tmp_path / "trusted_support.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef trusted_value():\n    return 7\n",
        encoding="utf-8",
    )
    (tmp_path / "test_dynamic_plugin.py").write_text(
        'globals()["pytest_plugins"] = ("candidate_filter",)\n\n'
        "def test_one():\n    assert True\n\n"
        "def test_two():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "test_trusted.py").write_text(
        'pytest_plugins = ("trusted_support",)\n\n'
        "def test_trusted(trusted_value):\n    assert trusted_value == 7\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    source_manifest = {
        "schema_version": 1,
        "candidate_sha": "a" * 40,
        "entries": [
            {
                "path": "test_dynamic_plugin.py",
                "blob_sha": "b" * 40,
                "source": (tmp_path / "test_dynamic_plugin.py").read_text(
                    encoding="utf-8"
                ),
            }
        ],
    }
    source_manifest_raw = json.dumps(
        source_manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    source_manifest_path = tmp_path / "candidate-test-sources.json"
    source_manifest_path.write_bytes(source_manifest_raw)
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_ENV] = str(source_manifest_path)
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV] = module.sha256_bytes(
        source_manifest_raw
    )
    environment[module.CANDIDATE_TEST_SOURCE_SHA_ENV] = "a" * 40
    base = [
        *module.pytest_entry_argv(
            Path(sys.executable),
            ("test_dynamic_plugin.py",),
        ),
        "-q",
        "-ra",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
        "-c",
        "/dev/null",
        "--rootdir",
        str(tmp_path),
        "--color=no",
    ]
    collected = subprocess.run(
        [*base, "--collect-only", "test_dynamic_plugin.py", "test_trusted.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    assert module.parse_collected_nodeids(
        collected.stdout,
        ["test_dynamic_plugin.py", "test_trusted.py"],
        10,
    ) == (
        "test_dynamic_plugin.py::test_one",
        "test_dynamic_plugin.py::test_two",
        "test_trusted.py::test_trusted",
    )

    executed = subprocess.run(
        [*base, "test_dynamic_plugin.py", "test_trusted.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert "3 passed" in executed.stdout


def test_candidate_pytest_launcher_freezes_changed_test_sources_before_execution(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    first = tmp_path / "test_a.py"
    second = tmp_path / "test_b.py"
    external = tmp_path / "external.py"
    first_source = (
        "from pathlib import Path\n"
        "target = Path(__file__).with_name('test_b.py')\n"
        "target.unlink()\n"
        "target.symlink_to(Path(__file__).with_name('external.py'))\n\n"
        "def test_a():\n    assert True\n"
    )
    second_source = (
        "from pathlib import Path\n\n"
        "def test_b():\n"
        "    target = Path(__file__)\n"
        "    target.unlink()\n"
        "    target.write_text('def test_b():\\n    assert True\\n', encoding='utf-8')\n"
        "    assert True\n"
    )
    first.write_text(first_source, encoding="utf-8")
    second.write_text(second_source, encoding="utf-8")
    external.write_text(
        "raise RuntimeError('external source executed')\n", encoding="utf-8"
    )
    payload = {
        "schema_version": 1,
        "candidate_sha": "a" * 40,
        "entries": [
            {"path": "test_a.py", "blob_sha": "b" * 40, "source": first_source},
            {"path": "test_b.py", "blob_sha": "c" * 40, "source": second_source},
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = tmp_path / "candidate-test-sources.json"
    manifest.write_bytes(raw)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_ENV] = str(manifest)
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV] = module.sha256_bytes(
        raw
    )
    environment[module.CANDIDATE_TEST_SOURCE_SHA_ENV] = "a" * 40
    launcher = module.pytest_entry_argv(
        Path(sys.executable), ("test_a.py", "test_b.py")
    )
    policy = [
        "-q",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
        "-c",
        "/dev/null",
        "--rootdir",
        str(tmp_path),
    ]

    combined = subprocess.run(
        [*launcher, *policy, "test_a.py", "test_b.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert combined.returncode != 0
    assert "cannot select multiple changed test modules" in combined.stderr

    first_executed = subprocess.run(
        [*launcher, *policy, "test_a.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first_executed.returncode == 0, first_executed.stderr
    assert "1 passed" in first_executed.stdout
    assert second.is_symlink()

    second_executed = subprocess.run(
        [*launcher, *policy, "test_b.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second_executed.returncode == 0, second_executed.stderr
    assert "1 passed" in second_executed.stdout
    assert not second.is_symlink()
    assert "external source executed" not in second_executed.stderr


def test_candidate_pytest_process_isolation_blocks_import_guard_replacement(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    first = tmp_path / "test_a.py"
    second = tmp_path / "test_b.py"
    first_source = (
        "from _pytest import python as _pytest_python\n\n"
        "def _candidate_import_bypass(*_args, **_kwargs):\n"
        "    raise RuntimeError('candidate import guard replacement invoked')\n\n"
        "_pytest_python.importtestmodule = _candidate_import_bypass\n\n"
        "def test_a():\n    assert True\n"
    )
    second_manifest_source = (
        "def test_b():\n    assert False, 'exact manifest source executed'\n"
    )
    first.write_text(first_source, encoding="utf-8")
    second.write_text("def test_b():\n    assert True\n", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "candidate_sha": "a" * 40,
        "entries": [
            {"path": "test_a.py", "blob_sha": "b" * 40, "source": first_source},
            {
                "path": "test_b.py",
                "blob_sha": "c" * 40,
                "source": second_manifest_source,
            },
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = tmp_path / "candidate-test-sources.json"
    manifest.write_bytes(raw)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_ENV] = str(manifest)
    environment[module.CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV] = module.sha256_bytes(
        raw
    )
    environment[module.CANDIDATE_TEST_SOURCE_SHA_ENV] = "a" * 40
    launcher = module.pytest_entry_argv(
        Path(sys.executable), ("test_a.py", "test_b.py")
    )
    policy = [
        "-q",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
        "-c",
        "/dev/null",
        "--rootdir",
        str(tmp_path),
    ]

    first_executed = subprocess.run(
        [*launcher, *policy, "test_a.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first_executed.returncode == 0, first_executed.stderr
    assert "1 passed" in first_executed.stdout

    second_executed = subprocess.run(
        [*launcher, *policy, "test_b.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second_executed.returncode == 1, second_executed.stderr
    assert "1 failed" in second_executed.stdout
    assert "exact manifest source executed" in second_executed.stdout
    assert "candidate import guard replacement invoked" not in second_executed.stderr


def test_candidate_pytest_orchestration_isolates_changed_modules() -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    first = "tests/unit/test_first.py"
    second = "tests/unit/test_second.py"
    runner.candidate_test_files = (first, second)
    runner.worktree = Path("/candidate")
    runner.profile = SimpleNamespace(pr_test_max_nodes=8, pr_test_python="3.12")
    collected: list[tuple[str, ...]] = []
    executed: list[tuple[str, ...]] = []

    def collect(
        _name,
        _python,
        selectors,
        *,
        cwd,
        env,
        max_nodes,
        failure_status,
        reject_filtered_collection=False,
        blocked_test_module_plugins=(),
    ):
        del cwd, env, max_nodes, failure_status
        assert reject_filtered_collection is True
        assert blocked_test_module_plugins == runner.candidate_test_files
        selected = tuple(selectors)
        collected.append(selected)
        assert len(selected) == 1
        return (f"{selected[0]}::test_one",)

    def run_exact(
        _name,
        _python,
        node_ids,
        expected_tests,
        *,
        env,
        blocked_test_module_plugins=(),
    ):
        del env
        assert blocked_test_module_plugins == runner.candidate_test_files
        selected = tuple(node_ids)
        assert expected_tests == len(selected) == 1
        executed.append(selected)

    runner._collect_pytest_nodes = collect
    runner._run_exact_pytest_nodes = run_exact
    python = Path("/trusted/venv/bin/python")
    candidate_nodes = runner._collect_candidate_pytest_nodes_isolated(python, {})
    runner._run_candidate_pytest_nodes_isolated(python, candidate_nodes, {})

    assert collected == [(first,), (second,)]
    assert executed == [
        (f"{first}::test_one",),
        (f"{second}::test_one",),
    ]


def _strict_collection_runner(
    module, tmp_path: Path, stdout: str, junit_data: dict
) -> Any:
    runner = module.Runner.__new__(module.Runner)
    runner.results = tmp_path
    runner.last_raw_stdout = stdout
    runner.captured_argv = []

    def fake_run_recorded(name, argv, *, cwd, env, timeout=None, junit=None):
        del name, cwd, env, timeout, junit
        runner.captured_argv.append(list(argv))
        return SimpleNamespace(returncode=0, junit=junit_data)

    runner._run_recorded = fake_run_recorded
    return runner


def test_candidate_collection_rejects_collection_time_skip(tmp_path: Path) -> None:
    module = assessment_module()
    stdout = "tests/unit/test_candidate.py::test_a\n1 test collected in 0.01s\n"
    junit = {
        "tests": 1,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "skip_details": [
            {"node_id": "tests/unit/test_candidate.py::test_a", "reason": "xfail"}
        ],
    }
    runner = _strict_collection_runner(module, tmp_path, stdout, junit)

    with pytest.raises(
        module.AssessmentError, match="omitted or filtered candidate tests"
    ) as exc:
        runner._collect_pytest_nodes(
            "candidate-regressions",
            tmp_path / "python",
            ["tests/unit/test_candidate.py::test_a"],
            cwd=tmp_path,
            env={},
            max_nodes=10,
            failure_status="FAIL",
            reject_filtered_collection=True,
        )

    assert exc.value.status == "FAIL"


def test_candidate_collection_rejects_reported_count_mismatch(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    stdout = "tests/unit/test_candidate.py::test_b\n2 tests collected in 0.01s\n"
    junit = {
        "tests": 2,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "skip_details": [],
    }
    runner = _strict_collection_runner(module, tmp_path, stdout, junit)

    with pytest.raises(
        module.AssessmentError, match="omitted or filtered candidate tests"
    ) as exc:
        runner._collect_pytest_nodes(
            "candidate-regressions",
            tmp_path / "python",
            ["tests/unit/test_candidate.py::test_b"],
            cwd=tmp_path,
            env={},
            max_nodes=10,
            failure_status="FAIL",
            reject_filtered_collection=True,
        )

    assert exc.value.status == "FAIL"


def test_candidate_collection_missing_summary_preserves_fail_status(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    stdout = "tests/unit/test_candidate.py::test_a\n"
    junit = {
        "tests": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "skip_details": [],
    }
    runner = _strict_collection_runner(module, tmp_path, stdout, junit)

    with pytest.raises(module.AssessmentError, match="summary is missing") as exc:
        runner._collect_pytest_nodes(
            "candidate-regressions",
            tmp_path / "python",
            ["tests/unit/test_candidate.py::test_a"],
            cwd=tmp_path,
            env={},
            max_nodes=10,
            failure_status="FAIL",
            reject_filtered_collection=True,
        )

    assert exc.value.status == "FAIL"


def test_exact_pytest_nodes_uses_candidate_guard_only_when_requested(
    tmp_path: Path,
) -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.results = tmp_path
    runner.worktree = tmp_path
    runner.profile = SimpleNamespace(expected_skips=0)
    runner.evidence = module.AssessmentEvidence()
    runner.failed_checks = False
    captured: list[list[str]] = []

    def fake_run_recorded(name, argv, *, cwd, env, timeout=None, junit=None):
        del cwd, env, timeout, junit
        captured.append(list(argv))
        return SimpleNamespace(
            name=name,
            junit={
                "tests": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "skip_details": [],
            },
            expected_tests=None,
            expected_skips=None,
            junit_check_passed=None,
        )

    runner._run_recorded = fake_run_recorded
    python = tmp_path / "python"
    node = "tests/unit/test_candidate.py::test_a"
    runner._run_exact_pytest_nodes(
        "guarded",
        python,
        [node],
        1,
        env={},
        blocked_test_module_plugins=("tests/unit/test_candidate.py",),
    )
    assert captured[-1][:4] == [
        str(python),
        "-P",
        "-c",
        module.CANDIDATE_PYTEST_LAUNCHER,
    ]

    runner._run_exact_pytest_nodes("trusted", python, [node], 1, env={})
    assert captured[-1][:3] == [str(python), "-m", "pytest"]


def test_pr_head_final_identity_rejects_moving_head(tmp_path: Path) -> None:
    module = assessment_module()
    requested = "a" * 40
    control = "b" * 40
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha=requested, fetch=True, expected_ref=None)
    runner.target_kind = "pr-head"
    runner.pr_number = 320
    runner.worktree = tmp_path
    runner.tools = {"git": "/usr/bin/git"}
    runner.evidence = module.AssessmentEvidence(
        target_kind="pr-head",
        pr_number=320,
        requested_sha=requested,
        pr_head_start=requested,
        control_sha=control,
        control_ref_start=control,
    )

    def fake_control(argv, **_kwargs):
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=requested + "\n")
        return SimpleNamespace(stdout="")

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "fetch":
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "origin/main") or args == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=control + "\n")
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return SimpleNamespace(stdout="")
        raise AssertionError(f"unexpected git command: {args}")

    runner._control = fake_control
    runner._git = fake_git
    runner._fetch_pr_head = lambda: "f" * 40

    with pytest.raises(module.AssessmentError, match="head moved") as exc:
        runner.final_identity()

    assert exc.value.status == "STALE"
    assert runner.evidence.pr_head_end == "f" * 40


def test_pr_head_final_identity_rejects_dirty_control_checkout(tmp_path: Path) -> None:
    module = assessment_module()
    requested = "a" * 40
    control = "b" * 40
    runner = module.Runner.__new__(module.Runner)
    runner.args = SimpleNamespace(sha=requested, fetch=True, expected_ref=None)
    runner.target_kind = "pr-head"
    runner.pr_number = 320
    runner.worktree = tmp_path
    runner.tools = {"git": "/usr/bin/git"}
    runner.evidence = module.AssessmentEvidence(
        target_kind="pr-head",
        pr_number=320,
        requested_sha=requested,
        pr_head_start=requested,
        control_sha=control,
        control_ref_start=control,
    )

    def fake_control(argv, **_kwargs):
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=requested + "\n")
        return SimpleNamespace(stdout="")

    def fake_git(*args: str, check: bool = True):
        del check
        if args and args[0] == "fetch":
            return SimpleNamespace(stdout="")
        if args == ("rev-parse", "origin/main") or args == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=control + "\n")
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return SimpleNamespace(
                stdout=" M tests/unit/test_local_agent_assessment.py\n"
            )
        raise AssertionError(f"unexpected git command: {args}")

    runner._control = fake_control
    runner._git = fake_git
    runner._fetch_pr_head = lambda: requested

    with pytest.raises(module.AssessmentError, match="became dirty") as exc:
        runner.final_identity()

    assert exc.value.status == "STALE"


def test_candidate_test_manifest_is_sorted_hashed_and_machine_verifiable() -> None:
    module = assessment_module()
    stdout = "tests/unit/test_b.py::test_z\ntests/unit/test_a.py::test_a\n2 tests collected in 0.01s"
    nodes = module.parse_collected_nodeids(
        stdout,
        ["tests/unit/test_a.py", "tests/unit/test_b.py"],
        10,
    )
    assert nodes == (
        "tests/unit/test_a.py::test_a",
        "tests/unit/test_b.py::test_z",
    )

    first = module.build_candidate_test_manifest(
        "c" * 40,
        ["tests/unit/test_b.py", "tests/unit/test_a.py"],
        list(reversed(nodes)),
    )
    second = module.build_candidate_test_manifest(
        "c" * 40,
        ["tests/unit/test_a.py", "tests/unit/test_b.py"],
        nodes,
    )
    assert first == second
    assert len(first["sha256"]) == 64


def test_pr_evidence_serialization_preserves_exact_identity_fields() -> None:
    module = assessment_module()
    manifest = module.build_candidate_test_manifest(
        "c" * 40,
        ["tests/unit/test_new.py"],
        ["tests/unit/test_new.py::test_new"],
    )
    evidence = module.AssessmentEvidence(
        target_kind="pr-head",
        pr_number=320,
        requested_sha="a" * 40,
        tested_sha="a" * 40,
        pr_head_start="a" * 40,
        pr_head_end="a" * 40,
        control_sha="b" * 40,
        control_ref_start="b" * 40,
        control_ref_end="b" * 40,
        candidate_test_manifest=manifest,
    )

    payload = json.loads(json.dumps(module.asdict(evidence)))
    assert payload["target_kind"] == "pr-head"
    assert payload["pr_number"] == 320
    assert payload["requested_sha"] == payload["tested_sha"] == "a" * 40
    assert payload["pr_head_start"] == payload["pr_head_end"] == "a" * 40
    assert payload["control_sha"] == "b" * 40
    assert payload["candidate_test_manifest"] == manifest


def test_control_fingerprint_ignores_candidate_control_plane(tmp_path: Path) -> None:
    module = assessment_module()
    candidate_runner = tmp_path / "scripts/local_agent_assessment.py"
    candidate_runner.parent.mkdir(parents=True)
    candidate_runner.write_text("raise SystemExit('candidate')\n", encoding="utf-8")

    runner = module.Runner.__new__(module.Runner)
    runner.control_root = ROOT
    runner.profile_path = ROOT / "references/local-agent-assessment-profiles.toml"
    runner.profile = module.load_profile(
        runner.profile_path,
        "phase1-control-policy",
    )
    runner.worktree = tmp_path

    fingerprints = runner._fingerprint_control_plane()
    assert fingerprints["runner"] == module.sha256_file(
        ROOT / "scripts/local_agent_assessment.py"
    )
    assert fingerprints["runner"] != module.sha256_file(candidate_runner)
    assert "static_policy" in fingerprints
    assert "static_baseline" in fingerprints


def test_pr_target_parser_exposes_only_bounded_identity_inputs() -> None:
    module = assessment_module()
    args = module.build_parser().parse_args(
        [
            "plan",
            "--sha",
            "a" * 40,
            "--profile",
            "phase1-control-policy",
            "--target-kind",
            "pr-head",
            "--pr",
            "320",
            "--fetch",
        ]
    )
    assert args.target_kind == "pr-head"
    assert args.pr == 320
    assert args.expected_ref is None


def test_sanitized_environment_does_not_inherit_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = assessment_module()
    monkeypatch.setenv("DATABASE_URL", "postgresql://production")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("BLOB_ROOT", "/persistent/blobs")

    environment = module.build_base_environment(
        tmp_path,
        {"git": "/usr/bin/git", "uv": "/usr/bin/uv"},
    )

    assert "DATABASE_URL" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "BLOB_ROOT" not in environment
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_service_json_is_strictly_allowlisted() -> None:
    module = assessment_module()
    parsed = module.parse_service_contract(service_payload(), "gate312-test")
    assert parsed["postgres"]["database"] == "gate312_test_test"

    payload = json.loads(service_payload())
    payload["environment"]["DATABASE_URL"] = "postgresql://production"
    with pytest.raises(
        ValueError, match="unexpected disposable-service environment keys"
    ):
        module.parse_service_contract(json.dumps(payload), "gate312-test")

    payload = json.loads(service_payload())
    payload["qdrant"]["port"] = 6333
    with pytest.raises(ValueError, match="Qdrant service metadata mismatch"):
        module.parse_service_contract(json.dumps(payload), "gate312-test")


def test_recovery_cli_requires_a_bounded_assessment_identity() -> None:
    module = assessment_module()
    args = module.build_parser().parse_args(
        ["recover", "--repo", str(ROOT), "--assessment-id", "gate312-test"]
    )
    assert args.command == "recover"
    assert args.assessment_id == "gate312-test"


@pytest.mark.parametrize(
    "selector",
    [
        "../tests/unit/test_example.py",
        "/tmp/test_example.py",
        "tests/unit/test_example.py;touch /tmp/oops",
        "scripts/test_example.py",
    ],
)
def test_profile_selector_rejects_command_and_path_escape(selector: str) -> None:
    module = assessment_module()
    with pytest.raises(ValueError, match="invalid pytest selector"):
        module.validate_selector(selector)


def test_workspace_paths_must_be_strict_descendants(tmp_path: Path) -> None:
    module = assessment_module()
    child = tmp_path / "worktrees" / "assessment"
    assert module.ensure_descendant(child, tmp_path) == child.resolve()
    with pytest.raises(ValueError, match="strict descendant"):
        module.ensure_descendant(tmp_path, tmp_path)
    with pytest.raises(ValueError, match="strict descendant"):
        module.ensure_descendant(tmp_path.parent / "outside", tmp_path)


def test_recovery_paths_must_match_assessment_identity_exactly(tmp_path: Path) -> None:
    module = assessment_module()
    expected = tmp_path / "materials" / "gate312-test"

    assert (
        module.require_exact_recovery_path(
            str(expected), expected, tmp_path, "materials"
        )
        == expected
    )
    with pytest.raises(module.AssessmentError, match="does not match"):
        module.require_exact_recovery_path(
            str(tmp_path / "materials" / "another-run"),
            expected,
            tmp_path,
            "materials",
        )
    expected.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    expected.symlink_to(target, target_is_directory=True)
    with pytest.raises(module.AssessmentError, match="symlink"):
        module.require_exact_recovery_path(
            str(expected), expected, tmp_path, "materials"
        )


def test_recovery_rejects_symlinked_parent(tmp_path: Path) -> None:
    module = assessment_module()
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "materials").symlink_to(target, target_is_directory=True)
    expected = tmp_path / "materials" / "gate312-test"

    with pytest.raises(module.AssessmentError, match="symlinked ancestor"):
        module.require_exact_recovery_path(
            str(expected), expected, tmp_path, "materials"
        )


def test_blob_inventory_hashes_content_and_detects_symlinks(tmp_path: Path) -> None:
    module = assessment_module()
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    link = tmp_path / "link"
    link.symlink_to(blob)

    observed = module.inventory(tmp_path)

    assert observed["blob"] == {
        "type": "file",
        "size": 7,
        "sha256": module.sha256_bytes(b"payload"),
    }
    assert observed["link"] == {"type": "symlink", "target": str(blob)}


def test_junit_summary_preserves_skip_reason(tmp_path: Path) -> None:
    module = assessment_module()
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuite><testcase file="tests/unit/test_x.py" name="test_ok"/>'
        '<testcase file="tests/unit/test_x.py" name="test_skip">'
        '<skipped message="requires disposable database"/></testcase></testsuite>',
        encoding="utf-8",
    )

    summary = module.junit_summary(report)
    assert summary == {
        "tests": 2,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "skip_details": [
            {
                "node_id": "tests/unit/test_x.py::test_skip",
                "reason": "requires disposable database",
            }
        ],
    }


def test_bounded_process_terminates_descendant_process_group(tmp_path: Path) -> None:
    module = assessment_module()
    marker = tmp_path / "descendant-survived"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.5); Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
    )

    outcome = module.run_bounded_process(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env=os.environ,
        timeout=0.1,
        terminate_grace_seconds=0.2,
    )

    assert outcome.returncode == 124
    assert outcome.timed_out is True
    time.sleep(0.7)
    assert not marker.exists()


def test_control_timeout_is_typed_blocked(tmp_path: Path) -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.repo = tmp_path
    runner.base_env = dict(os.environ)

    with pytest.raises(
        module.AssessmentError, match="control command timed out"
    ) as exc:
        runner._control(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.1,
        )

    assert exc.value.status == "BLOCKED"


def test_recorded_timeout_is_machine_visible_and_fails_check(tmp_path: Path) -> None:
    module = assessment_module()
    runner = module.Runner.__new__(module.Runner)
    runner.logs = tmp_path
    runner.profile = SimpleNamespace(command_timeout_seconds=30)
    runner.command_records = []
    runner.last_raw_stdout = ""
    runner.last_raw_stderr = ""
    runner.failed_checks = False

    record = runner._run_recorded(
        "timeout-probe",
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=0.1,
    )

    assert record.returncode == 124
    assert record.timed_out is True
    assert runner.failed_checks is True
    assert "command timed out" in Path(record.stderr_path).read_text(encoding="utf-8")


def test_recovery_lock_refusal_creates_no_assessment_materials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = assessment_module()
    assessment_id = "recovery-lock-test"
    repo = tmp_path / "repo"
    results = tmp_path / "results" / assessment_id
    worktree = tmp_path / "worktrees" / assessment_id
    materials = tmp_path / "materials" / assessment_id
    results.mkdir(parents=True)
    (results / "lifecycle.json").write_text(
        json.dumps(
            {
                "schema_version": module.LIFECYCLE_SCHEMA_VERSION,
                "assessment_id": assessment_id,
                "repo": str(repo.resolve()),
                "worktree": str(worktree),
                "materials": str(materials),
                "service_ports": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_AGENT_ASSESSMENT_ALLOWED_ROOT", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/true")

    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    lock_handle = (lock_dir / "host-assessment.lock").open("a+")
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(module.AssessmentError, match="another host assessment"):
            module.recover_abandoned(
                SimpleNamespace(
                    repo=str(repo),
                    assessment_id=assessment_id,
                    workspace_root=str(tmp_path),
                )
            )
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()

    assert not materials.exists()
