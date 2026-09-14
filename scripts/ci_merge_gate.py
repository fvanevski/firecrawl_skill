#!/usr/bin/env python3
"""Fail-closed aggregate Merge gate evaluator."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ci_authority import (
    REQUIRED_PROFILES,
    AuthorityError,
    changed_paths,
    plan_changed_paths,
    require_sha,
)

SUCCESS = "success"
PROFILE_EXECUTION_RECEIPT_SCHEMA = "ci-profile-execution-v1"
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
        return list(REQUIRED_PROFILES), [], "full", ["event:main"]
    if event != "pull_request":
        raise AuthorityError(f"unsupported CI planning event: {event}")

    selected, unknown = plan_changed_paths(repo, changed)
    reasons = gate_validation_escalation_reasons(changed)
    if reasons:
        selected = list(REQUIRED_PROFILES)
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
        REQUIRED_PROFILES
    ):
        failures.append("full_profile_completeness")

    observed_outcomes = dict(execution_outcomes or {})
    if set(observed_outcomes) != set(expected_matrix_profiles):
        failures.append("execution_profile_membership")
    else:
        expected_outcome = "unselected" if expected_matrix_profiles == ["__none__"] else SUCCESS
        if any(
            observed_outcomes.get(profile) != expected_outcome
            for profile in expected_matrix_profiles
        ):
            failures.append("execution_profile_outcome")

    executed_profiles = [
        name
        for name in REQUIRED_PROFILES
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


def load_execution_receipts(
    receipts_dir: Path,
    *,
    head_sha: str,
    run_id: int,
    run_attempt: int,
) -> dict[str, str]:
    """Load execution-derived matrix evidence for the current exact run."""

    if run_id < 1 or run_attempt < 1:
        raise AuthorityError("profile receipt run identity must be positive")
    if not receipts_dir.is_dir():
        raise AuthorityError(f"profile receipt directory is missing: {receipts_dir}")
    receipt_paths = sorted(receipts_dir.glob("ci-profile-receipt-*.json"))
    if not receipt_paths:
        raise AuthorityError("profile execution receipts are missing")

    allowed_profiles = set(REQUIRED_PROFILES) - {"static", "core"}
    allowed_profiles.add("__none__")
    outcomes: dict[str, str] = {}
    for path in receipt_paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise AuthorityError(f"profile receipt must be an object: {path.name}")
        if raw.get("schema_version") != PROFILE_EXECUTION_RECEIPT_SCHEMA:
            raise AuthorityError(f"profile receipt schema mismatch: {path.name}")
        if raw.get("head_sha") != head_sha:
            raise AuthorityError(f"profile receipt head mismatch: {path.name}")
        if raw.get("run_id") != run_id or raw.get("run_attempt") != run_attempt:
            raise AuthorityError(f"profile receipt run identity mismatch: {path.name}")

        profile = raw.get("profile")
        outcome = raw.get("outcome")
        if not isinstance(profile, str) or profile not in allowed_profiles:
            raise AuthorityError(f"profile receipt has invalid profile: {path.name}")
        if path.name != f"ci-profile-receipt-{profile}.json":
            raise AuthorityError(f"profile receipt filename mismatch: {path.name}")
        if profile in outcomes:
            raise AuthorityError(f"duplicate profile execution receipt: {profile}")
        if profile == "__none__":
            if outcome != "unselected":
                raise AuthorityError("unselected profile receipt must say unselected")
        elif outcome not in {"success", "failure", "cancelled", "skipped"}:
            raise AuthorityError(f"profile receipt outcome is invalid: {path.name}")
        outcomes[profile] = str(outcome)
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
    parser.add_argument("--profile-receipts-dir", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.selected_count < 0:
            raise AuthorityError("selected count must be non-negative")
        repo = Path(args.repo).resolve()
        base_sha = require_sha(args.base_sha, "base SHA")
        head_sha = require_sha(args.head_sha, "head SHA")
        paths = changed_paths(repo, base_sha, head_sha)
        required_profiles, unknown, required_scope, required_reasons = required_validation(
            repo,
            paths,
            event=args.event,
        )
        if unknown:
            raise AuthorityError(
                "merge-gate impact plan contains unknown/unmapped paths: "
                + ", ".join(unknown)
            )
        execution_outcomes = load_execution_receipts(
            Path(args.profile_receipts_dir).resolve(),
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
