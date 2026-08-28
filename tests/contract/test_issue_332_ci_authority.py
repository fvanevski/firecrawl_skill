"""Issue #332 centralized CI/test-authority regression contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CI = ROOT / "ci"
WORKFLOWS = ROOT / ".github" / "workflows"

sys.path.insert(0, str(SCRIPTS))
from ci_authority import (  # noqa: E402
    REQUIRED_PROFILES,
    build_baseline,
    load_profiles,
    plan_changed_paths,
    resolved_membership,
)


def _load_merge_gate_module():
    path = SCRIPTS / "ci_merge_gate.py"
    spec = importlib.util.spec_from_file_location("ci_merge_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python312_toolchain_is_single_central_authority() -> None:
    manifest = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines()
    assert "pytest==9.1.1" in manifest
    assert "ruff==0.16.5" in manifest
    assert "pyrefly==1.2.0" in manifest

    runtime = (ROOT / "requirements-research-store.txt").read_text(encoding="utf-8")
    assert not any(line.strip().startswith("pytest") for line in runtime.splitlines())
    assert not (ROOT / "requirements-typecheck.txt").exists()
    assert not (ROOT / "requirements-local-agent-assessment.in").exists()
    assert not (ROOT / "requirements-local-agent-assessment-py311.lock").exists()
    assert not (ROOT / "requirements-local-agent-assessment-py312.lock").exists()


def test_pre_refactor_baseline_matches_exact_implementation_base() -> None:
    config = tomllib.loads((CI / "pre-refactor-baseline.toml").read_text(encoding="utf-8"))
    assert config["implementation_base_sha"] == "865976e399b9dd41637ca89d3b0b6547b0605dca"
    baseline = build_baseline(ROOT)
    assert baseline["workflow_count"] == 23
    assert baseline["test_file_count"] == 147
    assert baseline["selector_count"] == 155
    assert set(config["workflow_paths"]) == {item["path"] for item in baseline["workflows"]}
    if config["canonical_sha256"]:
        assert config["canonical_sha256"] == baseline["sha256"]


def test_every_baseline_selector_has_exactly_one_profile_owner() -> None:
    membership, baseline = resolved_membership(
        ROOT,
        head_sha=_git_head(),
    )
    owners: dict[str, str] = {}
    for profile, selectors in membership.items():
        for selector in selectors:
            assert selector.expression not in owners
            owners[selector.expression] = profile
    for item in baseline["selectors"]:
        assert item["expression"] in owners
    assert set(REQUIRED_PROFILES) == set(membership)


def test_profile_and_impact_authority_is_single_runtime_and_fail_closed() -> None:
    profiles, _, _ = load_profiles(ROOT)
    config = tomllib.loads((CI / "test-profiles.toml").read_text(encoding="utf-8"))
    assert config["python_version"] == "3.12"
    assert config["toolchain_manifest"] == "requirements-ci.txt"
    assert set(profiles) == set(REQUIRED_PROFILES)

    selected, unknown = plan_changed_paths(ROOT, ["references/ci-authority.md"])
    assert unknown == []
    assert selected == ["static", "core", "tooling"]
    assert profiles["tooling"].services == ()

    selected, unknown = plan_changed_paths(ROOT, ["totally-unknown.bin"])
    assert selected == ["static", "core"]
    assert unknown == ["totally-unknown.bin"]


def test_active_workflow_inventory_is_consolidated_and_python312_only() -> None:
    active = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
    assert active == ["ci.yml", "pyrefly-baseline.yml", "release-campaign.yml", "targeted-review.yml"]
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "3.11" not in text
        assert "mypy" not in text.lower()


def test_ci_keeps_old_required_context_during_merge_gate_transition() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "\n  pyrefly:\n    name: Pyrefly\n" in workflow
    assert "\n  merge-gate:\n    name: Merge gate\n" in workflow
    assert "scripts/ci_plan.py" in workflow
    assert "scripts/run_ci_profile.py" in workflow
    assert "requirements-ci.txt" in workflow
    transition = tomllib.loads((CI / "merge-policy-transition.toml").read_text(encoding="utf-8"))
    assert transition["old_required_check"] == "Pyrefly"
    assert transition["new_required_check"] == "Merge gate"
    assert transition["transition_state"] == "pending-exact-head-proof"


def test_merge_gate_distinguishes_unselected_from_failed_profiles() -> None:
    module = _load_merge_gate_module()
    unselected = module.evaluate_gate(
        plan="success", static="success", core="success", profiles="success", selected_count=0
    )
    assert unselected["result"] == "PASS"
    assert unselected["profile_state"] == "unselected"

    failed = module.evaluate_gate(
        plan="success", static="success", core="success", profiles="failure", selected_count=1
    )
    assert failed["result"] == "FAIL"
    assert "profiles" in failed["failures"]


def test_targeted_review_is_generic_manual_exact_head_only() -> None:
    workflow = (WORKFLOWS / "targeted-review.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "candidate-sha:" in workflow
    assert "base-sha:" in workflow
    assert "profile:" in workflow
    assert '[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in workflow


def test_release_campaign_remains_manual_exact_main_and_credentialed() -> None:
    workflow = (WORKFLOWS / "release-campaign.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert 'test "$DISPATCH_REF" = "refs/heads/main"' in workflow
    assert 'test "$WORKFLOW_SHA" = "$CANDIDATE_SHA"' in workflow
    assert "secrets.DATABASE_URL" in workflow
    assert "audit-gates:" not in workflow
    assert "requirements-ci.txt" not in workflow


def test_local_assessment_control_uses_python312_and_central_tool_manifest() -> None:
    runner = (SCRIPTS / "local_agent_assessment.py").read_text(encoding="utf-8")
    profile = (ROOT / "references/local-agent-assessment-profiles.toml").read_text(encoding="utf-8")
    assert 'ALLOWED_PYTHONS = {"3.12"}' in runner
    assert '"toolchain_manifest": self.control_root / "requirements-ci.txt"' in runner
    assert "requirements-local-agent-assessment-py" not in runner
    assert 'python_versions = ["3.12"]' in profile
    assert 'python_versions = ["3.11", "3.12"]' not in profile


def _git_head() -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
