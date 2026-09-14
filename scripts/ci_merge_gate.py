#!/usr/bin/env python3
"""Fail-closed aggregate Merge gate evaluator."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ci_authority import AuthorityError, plan_changed_paths

SUCCESS = "success"
PROFILE_JOB_PREFIX = "Profile — "
GATE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GATE_REQUIRED_PROFILES = (
    "static",
    "core",
    "tooling",
    "storage",
    "acquisition",
    "orchestration",
    "controller",
    "retrieval",
    "assessment",
    "migration",
    "release",
    "maintenance",
)
# Deliberately independent from ci_authority.FULL_VALIDATION_AUTHORITY_PATHS.
# A single candidate planner-authority regression must not be able to narrow away
# validation of that same authority change.
GATE_FULL_VALIDATION_AUTHORITY_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "ci/impact-map.toml",
        "ci/pre-refactor-baseline.toml",
        "ci/test-profiles.toml",
        "scripts/ci_authority.py",
        "scripts/ci_merge_gate.py",
        "scripts/ci_plan.py",
        "scripts/run_ci_profile.py",
    }
)


def gate_require_sha(value: str, label: str) -> str:
    """Validate exact Git identity without relying on candidate CI authority."""

    if not GATE_SHA_RE.fullmatch(value):
        raise AuthorityError(f"{label} must be a lowercase 40-character SHA")
    return value


def gate_changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    """Discover changed paths directly from Git, independent of ci_authority."""

    base_sha = gate_require_sha(base_sha, "base SHA")
    head_sha = gate_require_sha(head_sha, "head SHA")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            "--diff-filter=ACMRD",
            base_sha,
            head_sha,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AuthorityError(
            "merge-gate git diff failed "
            f"({completed.returncode}): {completed.stderr.strip()}"
        )
    return sorted(path for path in completed.stdout.splitlines() if path)


def gate_validation_escalation_reasons(changed: Sequence[str]) -> list[str]:
    """Return gate-owned reasons requiring main-equivalent PR validation."""

    return [
        f"ci-authority-change:{path}"
        for path in sorted(set(changed) & GATE_FULL_VALIDATION_AUTHORITY_PATHS)
    ]


def required_validation(
    repo: Path,
    changed: Sequence[str],
    *,
    event: str,
) -> tuple[list[str], list[str], str, list[str]]:
    """Recompute required scope without reusing candidate plan escalation."""

    if event == "main":
        return list(GATE_REQUIRED_PROFILES), [], "full", ["event:main"]
    if event != "pull_request":
        raise AuthorityError(f"unsupported CI planning event: {event}")

    selected, unknown = plan_changed_paths(repo, changed)
    reasons = gate_validation_escalation_reasons(changed)
    if reasons:
        selected = list(GATE_REQUIRED_PROFILES)
    return selected, unknown, "full" if reasons else "selective", reasons


def evaluate_gate(
    *,
    plan: str,
    static: str,
    core: str,
    profiles: str,
    selected_count: int,
    validation_scope: str = "selective",
    selected_profiles: Sequence[str] = (),
    matrix_profiles: Sequence[str] = (),
    validation_escalation_reasons: Sequence[str] = (),
    execution_outcomes: Mapping[str, str] | None = None,
    required_validation_scope: str = "selective",
    required_profiles: Sequence[str] = (),
    required_escalation_reasons: Sequence[str] = (),
) -> dict[str, object]:
    statuses = {"plan": plan, "static": static, "core": core, "profiles": profiles}
    failures = [
        name for name in ("plan", "static", "core") if statuses[name] != SUCCESS
    ]
    profile_state = "unselected" if selected_count == 0 else profiles
    if selected_count > 0 and profiles != SUCCESS:
        failures.append("profiles")

    expected_matrix_profiles = [
        name for name in required_profiles if name not in {"static", "core"}
    ] or ["__none__"]
    expected_count = (
        0 if expected_matrix_profiles == ["__none__"] else len(expected_matrix_profiles)
    )
    if validation_scope not in {"selective", "full"}:
        failures.append("validation_scope_invalid")
    if validation_scope != required_validation_scope:
        failures.append("validation_scope_mismatch")
    if list(validation_escalation_reasons) != list(required_escalation_reasons):
        failures.append("validation_escalation_reasons")
    if list(selected_profiles) != list(required_profiles):
        failures.append("profile_membership")
    if list(matrix_profiles) != expected_matrix_profiles:
        failures.append("matrix_profile_membership")
    if selected_count != expected_count:
        failures.append("selected_profile_count")
    if required_validation_scope == "full" and list(selected_profiles) != list(
        GATE_REQUIRED_PROFILES
    ):
        failures.append("full_profile_completeness")

    observed_outcomes = dict(execution_outcomes or {})
    if set(observed_outcomes) != set(expected_matrix_profiles):
        failures.append("execution_profile_membership")
    else:
        expected_outcome = (
            "unselected" if expected_matrix_profiles == ["__none__"] else SUCCESS
        )
        if any(
            observed_outcomes.get(profile) != expected_outcome
            for profile in expected_matrix_profiles
        ):
            failures.append("execution_profile_outcome")

    executed_profiles = [
        name
        for name in GATE_REQUIRED_PROFILES
        if name not in {"static", "core"} and name in observed_outcomes
    ]
    if "__none__" in observed_outcomes:
        executed_profiles.append("__none__")

    failures = list(dict.fromkeys(failures))
    return {
        "schema_version": "ci-merge-gate-v3",
        "result": "PASS" if not failures else "FAIL",
        "validation_scope": validation_scope,
        "required_validation_scope": required_validation_scope,
        "validation_escalation_reasons": list(validation_escalation_reasons),
        "selected_profiles": list(selected_profiles),
        "matrix_profiles": list(matrix_profiles),
        "required_profiles": list(required_profiles),
        "required_matrix_profiles": expected_matrix_profiles,
        "executed_profiles": executed_profiles,
        "execution_outcomes": observed_outcomes,
        "selected_profile_count": selected_count,
        "profile_state": profile_state,
        "statuses": statuses,
        "failures": failures,
    }


def _string_list(value: str, label: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise AuthorityError(f"{label} must be a JSON array of strings")
    return parsed


def load_execution_jobs(
    job_evidence_path: Path,
    *,
    head_sha: str,
    run_id: int,
    run_attempt: int,
) -> dict[str, str]:
    """Load execution-derived matrix evidence from the current Actions run."""

    if run_id < 1 or run_attempt < 1:
        raise AuthorityError("profile job run identity must be positive")
    raw = json.loads(job_evidence_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AuthorityError("profile job evidence must be an object")
    jobs = raw.get("jobs")
    total_count = raw.get("total_count")
    if not isinstance(jobs, list) or not isinstance(total_count, int):
        raise AuthorityError("profile job evidence is malformed")
    if total_count != len(jobs):
        raise AuthorityError("profile job evidence is incomplete")

    allowed_profiles = set(GATE_REQUIRED_PROFILES) - {"static", "core"}
    allowed_profiles.add("__none__")
    outcomes: dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise AuthorityError("profile job entry must be an object")
        name = job.get("name")
        if not isinstance(name, str) or not name.startswith(PROFILE_JOB_PREFIX):
            continue
        profile = name.removeprefix(PROFILE_JOB_PREFIX)
        if profile not in allowed_profiles:
            raise AuthorityError(f"profile job has invalid profile: {name}")
        if profile in outcomes:
            raise AuthorityError(f"duplicate profile execution job: {profile}")
        if job.get("head_sha") != head_sha:
            raise AuthorityError(f"profile job head mismatch: {name}")
        if job.get("run_id") != run_id or job.get("run_attempt") != run_attempt:
            raise AuthorityError(f"profile job run identity mismatch: {name}")

        steps = job.get("steps")
        if not isinstance(steps, list):
            raise AuthorityError(f"profile job steps are missing: {name}")
        expected_step = (
            "Record unselected profile state"
            if profile == "__none__"
            else "Run selected profile"
        )
        matching_steps = [
            step
            for step in steps
            if isinstance(step, dict) and step.get("name") == expected_step
        ]
        if len(matching_steps) != 1:
            raise AuthorityError(f"profile execution step is ambiguous: {name}")
        job_conclusion = job.get("conclusion")
        step_conclusion = matching_steps[0].get("conclusion")
        if profile == "__none__":
            outcomes[profile] = (
                "unselected"
                if job_conclusion == SUCCESS and step_conclusion == SUCCESS
                else str(step_conclusion or job_conclusion or "unknown")
            )
        else:
            outcomes[profile] = (
                SUCCESS
                if job_conclusion == SUCCESS and step_conclusion == SUCCESS
                else str(step_conclusion or job_conclusion or "unknown")
            )
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--event", choices=("pull_request", "main"), required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--static", required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--selected-count", type=int, required=True)
    parser.add_argument("--validation-scope", required=True)
    parser.add_argument("--validation-escalation-reasons-json", required=True)
    parser.add_argument("--selected-profiles-json", required=True)
    parser.add_argument("--matrix-profiles-json", required=True)
    parser.add_argument("--profile-jobs-json", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.selected_count < 0:
            raise AuthorityError("selected count must be non-negative")
        repo = Path(args.repo).resolve()
        base_sha = gate_require_sha(args.base_sha, "base SHA")
        head_sha = gate_require_sha(args.head_sha, "head SHA")
        paths = gate_changed_paths(repo, base_sha, head_sha)
        (
            required_profiles,
            unknown,
            required_scope,
            required_reasons,
        ) = required_validation(
            repo,
            paths,
            event=args.event,
        )
        if unknown:
            raise AuthorityError(
                "merge-gate impact plan contains unknown/unmapped paths: "
                + ", ".join(unknown)
            )
        execution_outcomes = load_execution_jobs(
            Path(args.profile_jobs_json).resolve(),
            head_sha=head_sha,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        result = evaluate_gate(
            plan=args.plan,
            static=args.static,
            core=args.core,
            profiles=args.profiles,
            selected_count=args.selected_count,
            validation_scope=args.validation_scope,
            selected_profiles=_string_list(
                args.selected_profiles_json, "selected profiles"
            ),
            matrix_profiles=_string_list(args.matrix_profiles_json, "matrix profiles"),
            validation_escalation_reasons=_string_list(
                args.validation_escalation_reasons_json,
                "validation escalation reasons",
            ),
            execution_outcomes=execution_outcomes,
            required_validation_scope=required_scope,
            required_profiles=required_profiles,
            required_escalation_reasons=required_reasons,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["result"] == "PASS" else 1
    except (AuthorityError, json.JSONDecodeError) as exc:
        print(f"ci-merge-gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
