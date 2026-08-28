#!/usr/bin/env python3
"""Fail-closed aggregate Merge gate evaluator."""

from __future__ import annotations

import argparse
import json
import sys

SUCCESS = "success"


def evaluate_gate(
    *,
    plan: str,
    static: str,
    core: str,
    profiles: str,
    selected_count: int,
) -> dict[str, object]:
    statuses = {"plan": plan, "static": static, "core": core, "profiles": profiles}
    failures = [name for name in ("plan", "static", "core") if statuses[name] != SUCCESS]
    profile_state = "unselected" if selected_count == 0 else profiles
    if selected_count > 0 and profiles != SUCCESS:
        failures.append("profiles")
    return {
        "schema_version": "ci-merge-gate-v1",
        "result": "PASS" if not failures else "FAIL",
        "selected_profile_count": selected_count,
        "profile_state": profile_state,
        "statuses": statuses,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--static", required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--selected-count", type=int, required=True)
    args = parser.parse_args()
    if args.selected_count < 0:
        print("selected count must be non-negative", file=sys.stderr)
        return 2
    result = evaluate_gate(
        plan=args.plan,
        static=args.static,
        core=args.core,
        profiles=args.profiles,
        selected_count=args.selected_count,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
