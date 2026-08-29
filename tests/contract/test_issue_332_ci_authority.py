"""Issue #332 centralized CI/test-authority regression contract."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CI = ROOT / "ci"
WORKFLOWS = ROOT / ".github" / "workflows"

sys.path.insert(0, str(SCRIPTS))
_ci_authority = importlib.import_module("ci_authority")
_run_ci_profile = importlib.import_module("run_ci_profile")
REQUIRED_PROFILES = _ci_authority.REQUIRED_PROFILES
build_baseline = _ci_authority.build_baseline
load_profiles = _ci_authority.load_profiles
plan_changed_paths = _ci_authority.plan_changed_paths
resolved_membership = _ci_authority.resolved_membership
AuthorityError = _ci_authority.AuthorityError
validate_ruff_debt = _run_ci_profile.validate_ruff_debt


def _load_merge_gate_module():
    path = SCRIPTS / "ci_merge_gate.py"
    spec = importlib.util.spec_from_file_location("ci_merge_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transition_state() -> str:
    transition = tomllib.loads(
        (CI / "merge-policy-transition.toml").read_text(encoding="utf-8")
    )
    return str(transition["transition_state"])


def test_python312_toolchain_is_single_central_authority() -> None:
    manifest = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines()
    assert "pytest==9.1.1" in manifest
    assert "ruff==0.16.5" in manifest
    assert "pyrefly==1.2.0" in manifest

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["ruff"]["target-version"] == "py312"
    assert project["tool"]["ruff"]["extend-exclude"] == ["*.md"]
    assert project["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]

    runtime = (ROOT / "requirements-research-store.txt").read_text(encoding="utf-8")
    assert not any(line.strip().startswith("pytest") for line in runtime.splitlines())

    legacy = (
        ROOT / "requirements-typecheck.txt",
        ROOT / "requirements-local-agent-assessment.in",
        ROOT / "requirements-local-agent-assessment-py311.lock",
        ROOT / "requirements-local-agent-assessment-py312.lock",
    )
    if _transition_state() == "pending-exact-head-proof":
        # The old topology remains executable only during the required parallel
        # equivalence window. New CI/local authority must not consume these files.
        assert all(path.exists() for path in legacy)
    else:
        assert not any(path.exists() for path in legacy)

    runner = (SCRIPTS / "local_agent_assessment.py").read_text(encoding="utf-8")
    assert "requirements-local-agent-assessment-py" not in runner
    for workflow_name in (
        "ci.yml",
        "pyrefly-baseline.yml",
        "release-campaign.yml",
        "targeted-review.yml",
    ):
        workflow = (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
        assert "requirements-typecheck.txt" not in workflow
        assert "requirements-local-agent-assessment" not in workflow


def test_pre_refactor_baseline_matches_exact_implementation_base() -> None:
    config = tomllib.loads(
        (CI / "pre-refactor-baseline.toml").read_text(encoding="utf-8")
    )
    assert (
        config["implementation_base_sha"] == "865976e399b9dd41637ca89d3b0b6547b0605dca"
    )
    baseline = build_baseline(ROOT)
    assert baseline["workflow_count"] == 23
    assert baseline["test_file_count"] == 147
    assert baseline["selector_count"] == 155
    assert set(config["workflow_paths"]) == {
        item["path"] for item in baseline["workflows"]
    }
    delegated = next(
        item
        for item in baseline["selectors"]
        if item["expression"] == "tests/contract/test_release_secret_scan.py"
    )
    assert set(delegated["workflows"]) == {
        ".github/workflows/audit-release-gates.yml",
        ".github/workflows/release-campaign.yml",
    }
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


def test_service_backed_historical_selectors_do_not_fall_into_core() -> None:
    expected_owners = {
        "tests/acceptance/test_extraction_e2e.py": "acquisition",
        "tests/integration/test_audit_persistence.py": "storage",
        "tests/integration/test_authoritative_smart_validation.py": "acquisition",
        "tests/integration/test_curated_run_integration.py": "orchestration",
        "tests/integration/test_explicit_export_reproducibility.py": "release",
        "tests/integration/test_issue_214_search_relational_provenance.py": "acquisition",
        "tests/integration/test_issue_215_completion_budget.py": "acquisition",
        "tests/integration/test_issue_261_review_remediation.py": "orchestration",
    }
    profiles, _, _ = load_profiles(ROOT)
    membership, _ = resolved_membership(ROOT, head_sha=_git_head())
    for path, owner in expected_owners.items():
        assert profiles[owner].services
        assert any(selector.base_path == path for selector in membership[owner])
        assert not any(selector.base_path == path for selector in membership["core"])
        selected, unknown = plan_changed_paths(ROOT, [path])
        assert unknown == []
        assert owner in selected


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


def test_representative_impact_plans_preserve_architecture_dependencies() -> None:
    cases = {
        "src/firecrawl_skill/research_store/acquisition/service.py": [
            "static",
            "core",
            "storage",
            "acquisition",
            "orchestration",
            "controller",
        ],
        "src/firecrawl_skill/research_store/orchestration/executor.py": [
            "static",
            "core",
            "storage",
            "orchestration",
            "controller",
        ],
        "src/firecrawl_skill/research_store/research_controller.py": [
            "static",
            "core",
            "storage",
            "orchestration",
            "controller",
        ],
        "src/firecrawl_skill/research_store/retrieval/service.py": [
            "static",
            "core",
            "storage",
            "orchestration",
            "retrieval",
        ],
        "src/firecrawl_skill/research_store/alembic/versions/0045_operator_actions.py": [
            "static",
            "core",
            "storage",
            "orchestration",
            "migration",
        ],
        "scripts/fresearch": ["static", "core", "tooling", "controller"],
    }
    for path, expected in cases.items():
        selected, unknown = plan_changed_paths(ROOT, [path])
        assert unknown == [], path
        assert selected == expected, path


def test_ci_profiles_validate_the_installed_canonical_package() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    targeted = (WORKFLOWS / "targeted-review.yml").read_text(encoding="utf-8")
    install = "python -m pip install --no-deps -e ."
    assert workflow.count(install) == 3
    assert targeted.count(install) == 1
    assert workflow.index("Install canonical CI toolchain") < workflow.index(
        "Install canonical package"
    )
    assert targeted.index("Install canonical CI toolchain") < targeted.index(
        "Install canonical package"
    )


def test_static_scope_is_full_repository_plus_extensionless_entrypoint() -> None:
    runner = (SCRIPTS / "run_ci_profile.py").read_text(encoding="utf-8")
    assert '"E402,E731"' in runner
    assert 'verify_ruff_debt(repo, E402_DEBT_PATH, "E402")' in runner
    assert 'verify_ruff_debt(repo, E731_DEBT_PATH, "E731")' in runner
    assert 'run(["ruff", "format", "--check", "--diff", "."], cwd=repo)' in runner
    assert 'EXTENSIONLESS_STATIC_TARGETS = ("scripts/fsearch_smart",)' in runner
    assert 'run(["pyrefly", "check", "--output-format=full-text"], cwd=repo)' in runner
    assert '"--diff-filter=ACMR"' not in runner
    assert 'raise AuthorityError("static profile requires --base-sha")' in runner

    ci_workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    targeted = (WORKFLOWS / "targeted-review.yml").read_text(encoding="utf-8")
    assert '--base-sha "$BASE_SHA"' in ci_workflow
    assert '--base-sha "$BASE_SHA"' in targeted


@pytest.mark.parametrize("diagnostic_code", ["E402", "E731"])
def test_ruff_debt_contract_fails_closed_on_any_inventory_drift(
    diagnostic_code: str,
) -> None:
    stable = {"tests/example.py": 2, "scripts/example.py": 1}
    validate_ruff_debt(diagnostic_code, stable, stable)
    with pytest.raises(AuthorityError, match=rf"Ruff {diagnostic_code} debt drift"):
        validate_ruff_debt(diagnostic_code, stable, {**stable, "tests/new.py": 1})
    with pytest.raises(AuthorityError, match=rf"Ruff {diagnostic_code} debt drift"):
        validate_ruff_debt(diagnostic_code, stable, {"tests/example.py": 2})
    with pytest.raises(AuthorityError, match=rf"Ruff {diagnostic_code} debt drift"):
        validate_ruff_debt(diagnostic_code, stable, {**stable, "tests/example.py": 3})


@pytest.mark.parametrize(
    ("filename", "diagnostic_code"),
    [("ruff-e402-debt.toml", "E402"), ("ruff-e731-debt.toml", "E731")],
)
def test_ruff_debt_contracts_are_exact_not_glob_based(
    filename: str, diagnostic_code: str
) -> None:
    debt = tomllib.loads((CI / filename).read_text(encoding="utf-8"))
    assert debt["schema_version"] == 1
    assert debt["diagnostic_code"] == diagnostic_code
    assert isinstance(debt["counts"], dict)
    assert all(
        path.endswith(".py") and "*" not in path and "?" not in path
        for path in debt["counts"]
    )


def test_e731_debt_is_only_the_main_owned_host_regression_exception() -> None:
    debt = tomllib.loads((CI / "ruff-e731-debt.toml").read_text(encoding="utf-8"))
    assert debt["counts"] == {"tests/integration/test_acquisition_authority.py": 1}


def test_active_workflow_inventory_is_consolidated_and_python312_only() -> None:
    retained = {
        "ci.yml",
        "pyrefly-baseline.yml",
        "release-campaign.yml",
        "targeted-review.yml",
    }
    active = {path.name for path in WORKFLOWS.glob("*.yml")}
    state = _transition_state()
    if state == "pending-exact-head-proof":
        baseline = tomllib.loads(
            (CI / "pre-refactor-baseline.toml").read_text(encoding="utf-8")
        )
        historical = {Path(path).name for path in baseline["workflow_paths"]}
        assert active == historical | {"targeted-review.yml"}
    else:
        assert active == retained

    for workflow_name in retained:
        text = (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
        assert "3.11" not in text
        assert "mypy" not in text.lower()


def test_operations_runbook_release_evidence_is_python312_only() -> None:
    runbook = (ROOT / "references" / "operations-runbook.md").read_text(
        encoding="utf-8"
    )
    release_section = runbook.split("## 13. Release evidence", 1)[1].split(
        "## 14. Destructive commands", 1
    )[0]
    assert "Python 3.12" in release_section
    assert "3.11" not in release_section


def test_ci_emits_static_and_merge_gate_after_policy_cutover() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "\n  pyrefly:\n    name: Pyrefly\n" in workflow
    assert "\n  merge-gate:\n    name: Merge gate\n" in workflow
    assert "scripts/ci_plan.py" in workflow
    assert "scripts/run_ci_profile.py" in workflow
    assert "requirements-ci.txt" in workflow
    transition = tomllib.loads(
        (CI / "merge-policy-transition.toml").read_text(encoding="utf-8")
    )
    assert transition["old_required_check"] == "Pyrefly"
    assert transition["new_required_check"] == "Merge gate"
    assert transition["transition_state"] == "complete"


def test_merge_gate_distinguishes_unselected_from_failed_profiles() -> None:
    module = _load_merge_gate_module()
    unselected = module.evaluate_gate(
        plan="success",
        static="success",
        core="success",
        profiles="success",
        selected_count=0,
    )
    assert unselected["result"] == "PASS"
    assert unselected["profile_state"] == "unselected"

    failed = module.evaluate_gate(
        plan="success",
        static="success",
        core="success",
        profiles="failure",
        selected_count=1,
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
    assert '--output "$RUNNER_TEMP/targeted-plan.json"' in workflow
    assert '--output targeted-plan.json' not in workflow
    assert '${{ runner.temp }}/targeted-plan.json' in workflow


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
    profile = (ROOT / "references/local-agent-assessment-profiles.toml").read_text(
        encoding="utf-8"
    )
    assert 'ALLOWED_PYTHONS = {"3.12"}' in runner
    assert '"toolchain_manifest": self.control_root / "requirements-ci.txt"' in runner
    assert (
        '"pytest_skip_allowlist": self.control_root / PYTEST_SKIP_ALLOWLIST_PATH'
        in runner
    )
    assert (
        '"pytest_skip_verifier": self.control_root / PYTEST_SKIP_VERIFIER_PATH'
        in runner
    )
    assert "requirements-local-agent-assessment-py" not in runner
    assert 'python_versions = ["3.12"]' in profile
    assert 'python_versions = ["3.11", "3.12"]' not in profile


def _git_head() -> str:
    import subprocess

    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
