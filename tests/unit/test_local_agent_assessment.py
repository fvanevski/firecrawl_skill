from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

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

    with pytest.raises(module.AssessmentError, match="control command timed out") as exc:
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
