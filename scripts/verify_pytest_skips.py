"""Verify that every pytest skip is explicit, classified, and expected."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "pytest-skip-report-v1"
_ALLOWLIST_SCHEMA_VERSION = "pytest-skip-allowlist-v1"


def _node_id(testcase: ET.Element) -> str:
    file_name = testcase.get("file")
    class_name = testcase.get("classname", "")
    test_name = testcase.get("name", "")
    if not test_name:
        raise ValueError("JUnit testcase is missing its name")

    class_parts = [part for part in class_name.split(".") if part]
    test_class = None
    if class_parts and class_parts[-1].startswith("Test"):
        test_class = class_parts.pop()

    if not file_name:
        if not class_parts:
            raise ValueError(f"cannot derive file path for JUnit testcase {test_name!r}")
        file_name = "/".join(class_parts) + ".py"

    parts = [file_name]
    if test_class:
        parts.append(test_class)
    parts.append(test_name)
    return "::".join(parts)


def _skips(report_path: Path) -> list[dict[str, str]]:
    root = ET.parse(report_path).getroot()
    result: list[dict[str, str]] = []
    for testcase in root.iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is None:
            continue
        reason = (skipped.get("message") or skipped.text or "").strip()
        result.append({"node_id": _node_id(testcase), "reason": reason})
    return sorted(result, key=lambda item: item["node_id"])


def verify(
    report_path: Path,
    allowlist_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    allowlist = json.loads(allowlist_path.read_text(encoding="utf-8"))
    if allowlist.get("schema_version") != _ALLOWLIST_SCHEMA_VERSION:
        raise ValueError("unsupported pytest skip allowlist schema")

    entries = allowlist.get("entries")
    if not isinstance(entries, list):
        raise TypeError("pytest skip allowlist entries must be a list")
    expected: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("pytest skip allowlist entry must be an object")
        node_id = str(entry.get("node_id") or "")
        reason_contains = str(entry.get("reason_contains") or "")
        classification = str(entry.get("classification") or "")
        replacement_gate = str(entry.get("replacement_gate") or "")
        if not all((node_id, reason_contains, classification, replacement_gate)):
            raise ValueError("pytest skip allowlist entries require all fields")
        if node_id in expected:
            raise ValueError(f"duplicate pytest skip allowlist node: {node_id}")
        expected[node_id] = {
            "reason_contains": reason_contains,
            "classification": classification,
            "replacement_gate": replacement_gate,
        }

    actual = _skips(report_path)
    actual_by_id = {item["node_id"]: item for item in actual}
    if len(actual_by_id) != len(actual):
        raise ValueError("JUnit report contains duplicate skipped node IDs")

    unknown = sorted(set(actual_by_id) - set(expected))
    stale = sorted(set(expected) - set(actual_by_id))
    reason_mismatches = []
    classified = []
    for node_id in sorted(set(actual_by_id) & set(expected)):
        reason = actual_by_id[node_id]["reason"]
        rule = expected[node_id]
        if rule["reason_contains"] not in reason:
            reason_mismatches.append(
                {
                    "node_id": node_id,
                    "expected_substring": rule["reason_contains"],
                    "actual_reason": reason,
                }
            )
        classified.append(
            {
                "node_id": node_id,
                "reason": reason,
                "classification": rule["classification"],
                "replacement_gate": rule["replacement_gate"],
            }
        )

    status = "passed" if not (unknown or stale or reason_mismatches) else "failed"
    result: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "report": str(report_path),
        "allowlist": str(allowlist_path),
        "skip_count": len(actual),
        "classified_skips": classified,
        "unknown_skips": unknown,
        "stale_allowlist_entries": stale,
        "reason_mismatches": reason_mismatches,
    }
    if output_path is not None:
        output_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if status != "passed":
        raise ValueError(json.dumps(result, sort_keys=True))
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--junitxml", required=True, type=Path)
    result.add_argument("--allowlist", required=True, type=Path)
    result.add_argument("--output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = verify(args.junitxml, args.allowlist, args.output)
    except (OSError, ET.ParseError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"pytest skip verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
