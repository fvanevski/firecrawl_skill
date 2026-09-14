#!/usr/bin/env python3
"""Fail-closed aggregate Merge gate evaluator."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ci_authority import (
    REQUIRED_PROFILES,
    AuthorityError,
    changed_paths,
    plan_validation,
    require_sha,
)

SUCCESS = "success"


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
    expected_count = 0 if expected_matrix_profiles == ["__none__"] else len(
        expected_matrix_profiles
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

    failures = list(dict.fromkeys(failures))
    return {
        "schema_version": "ci-merge-gate-v2",
        "result": "PASS" if not failures else "FAIL",
        "validation_scope": validation_scope,
        "required_validation_scope": required_validation_scope,
        "validation_escalation_reasons": list(validation_escalation_reasons),
        "selected_profiles": list(selected_profiles),
        "matrix_profiles": list(matrix_profiles),
        "required_profiles": list(required_profiles),
        "required_matrix_profiles": expected_matrix_profiles,
        "selected_profile_count": selected_count,
        "profile_state": profile_state,
        "statuses": statuses,
        "failures": failures,
    }


def _string_list(value: str, label: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise AuthorityError(f"{label} must be a JSON array of strings")
    return parsed


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
    args = parser.parse_args()
    try:
        if args.selected_count < 0:
            raise AuthorityError("selected count must be non-negative")
        repo = Path(args.repo).resolve()
        base_sha = require_sha(args.base_sha, "base SHA")
        head_sha = require_sha(args.head_sha, "head SHA")
        paths = changed_paths(repo, base_sha, head_sha)
        required_profiles, unknown, required_scope, required_reasons = plan_validation(
            repo,
            paths,
            event=args.event,
        )
        if unknown:
            raise AuthorityError(
                "merge-gate impact plan contains unknown/unmapped paths: "
                + ", ".join(unknown)
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
