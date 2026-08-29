from __future__ import annotations

import json
from pathlib import Path

import pytest
from verify_pytest_skips import verify


def _write_junit(path: Path, cases: str) -> None:
    path.write_text(
        f'<testsuites><testsuite name="suite">{cases}</testsuite></testsuites>',
        encoding="utf-8",
    )


def _write_allowlist(path: Path, entries: list[dict[str, str]]) -> None:
    path.write_text(
        json.dumps({"schema_version": "pytest-skip-allowlist-v1", "entries": entries}),
        encoding="utf-8",
    )


def _entry(node_id: str, reason: str) -> dict[str, str]:
    return {
        "node_id": node_id,
        "reason_contains": reason,
        "classification": "external-integration",
        "replacement_gate": "Real release campaign",
    }


def test_verify_accepts_exact_classified_skip_set(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    output = tmp_path / "skip-report.json"
    _write_junit(
        report,
        '<testcase classname="scripts.test_example.TestCase" name="test_external" '
        'file="scripts/test_example.py"><skipped message="requires service"/>'
        "</testcase>",
    )
    _write_allowlist(
        allowlist,
        [
            _entry(
                "scripts/test_example.py::TestCase::test_external", "requires service"
            )
        ],
    )

    result = verify(report, allowlist, output)

    assert result["status"] == "passed"
    assert result["skip_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_verify_rejects_unknown_skip(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    _write_junit(
        report,
        '<testcase classname="scripts.test_example" name="test_unknown">'
        '<skipped message="unexpected"/></testcase>',
    )
    _write_allowlist(allowlist, [])

    with pytest.raises(ValueError, match="unknown_skips"):
        verify(report, allowlist)


def test_verify_rejects_stale_allowlist_entry(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    _write_junit(report, '<testcase classname="scripts.test_example" name="test_ok"/>')
    _write_allowlist(
        allowlist,
        [_entry("scripts/test_example.py::test_external", "requires service")],
    )

    with pytest.raises(ValueError, match="stale_allowlist_entries"):
        verify(report, allowlist)


def test_verify_scoped_report_ignores_allowlist_entries_outside_execution_scope(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    _write_junit(
        report,
        '<testcase classname="tests.test_selected" name="test_ok" '
        'file="tests/test_selected.py"/>',
    )
    _write_allowlist(
        allowlist,
        [_entry("tests/test_other.py::test_external", "requires service")],
    )

    result = verify(
        report,
        allowlist,
        scope_selectors=["tests/test_selected.py"],
    )

    assert result["status"] == "passed"
    assert result["stale_allowlist_entries"] == []
    assert result["scope_selectors"] == ["tests/test_selected.py"]


def test_verify_scoped_report_still_rejects_stale_entry_inside_scope(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    _write_junit(
        report,
        '<testcase classname="tests.test_selected" name="test_ok" '
        'file="tests/test_selected.py"/>',
    )
    _write_allowlist(
        allowlist,
        [_entry("tests/test_selected.py::test_external", "requires service")],
    )

    with pytest.raises(ValueError, match="stale_allowlist_entries"):
        verify(
            report,
            allowlist,
            scope_selectors=["tests/test_selected.py"],
        )


def test_verify_rejects_reason_drift(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    allowlist = tmp_path / "allowlist.json"
    _write_junit(
        report,
        '<testcase classname="scripts.test_example" name="test_external">'
        '<skipped message="different reason"/></testcase>',
    )
    _write_allowlist(
        allowlist,
        [_entry("scripts/test_example.py::test_external", "requires service")],
    )

    with pytest.raises(ValueError, match="reason_mismatches"):
        verify(report, allowlist)
