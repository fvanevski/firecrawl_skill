#!/usr/bin/env python3
"""Produce the exact deterministic CI profile plan for one base/head pair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ci_authority import (
    AuthorityError,
    REQUIRED_PROFILES,
    changed_paths,
    plan_changed_paths,
    require_sha,
    validate_authority,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--event", choices=("pull_request", "main"), required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = Path(args.repo).resolve()
    try:
        base_sha = require_sha(args.base_sha, "base SHA")
        head_sha = require_sha(args.head_sha, "head SHA")
        authority = validate_authority(repo, head_sha=head_sha)
        paths = changed_paths(repo, base_sha, head_sha)
        if args.event == "main":
            selected = list(REQUIRED_PROFILES)
            unknown: list[str] = []
        else:
            selected, unknown = plan_changed_paths(repo, paths)
        if unknown:
            raise AuthorityError(
                "impact plan contains unknown/unmapped paths: " + ", ".join(unknown)
            )
        selected_non_core = [
            name for name in selected if name not in {"static", "core"}
        ]
        matrix_profiles = selected_non_core or ["__none__"]
        payload = {
            "schema_version": "ci-plan-v1",
            "event": args.event,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_paths": paths,
            "unknown_paths": unknown,
            "selected_profiles": selected,
            "selected_non_core_profiles": selected_non_core,
            "selected_non_core_count": len(selected_non_core),
            "matrix_profiles": matrix_profiles,
            "baseline_sha": authority["baseline"]["implementation_base_sha"],
            "baseline_sha256": authority["baseline"]["sha256"],
            "baseline_counts": {
                "workflows": authority["baseline"]["workflow_count"],
                "test_files": authority["baseline"]["test_file_count"],
                "selectors": authority["baseline"]["selector_count"],
            },
            "profile_services": authority["profile_services"],
            "resolved_membership": authority["membership"],
            "execution_membership": authority["execution_membership"],
        }
        output = Path(args.output)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except AuthorityError as exc:
        print(f"ci-plan: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
