"""Fail unless a named corrective pytest case was collected and passed."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def verify_case(junitxml: Path, test_name: str) -> list[str]:
    root = ET.parse(junitxml).getroot()
    cases = [case for case in root.iter("testcase") if case.get("name") == test_name]
    if len(cases) != 1:
        return [f"expected exactly one {test_name!r} testcase, found {len(cases)}"]
    case = cases[0]
    failures = [
        child.tag for child in case if child.tag in {"skipped", "failure", "error"}
    ]
    if failures:
        return [f"{test_name!r} did not pass: {', '.join(failures)}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, required=True)
    parser.add_argument("--test-name", required=True)
    args = parser.parse_args(argv)
    errors = verify_case(args.junitxml, args.test_name)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
