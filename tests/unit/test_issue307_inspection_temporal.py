"""Issue #307: bounded temporal fields in inspection payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.inspection_service import InspectionService


class FakeCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.current = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement, params=()):
        self.statements.append((" ".join(statement.split()), tuple(params)))
        self.current = next(self.responses)

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, responses):
        self.cursor_value = FakeCursor(responses)

    def cursor(self):
        return self.cursor_value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def service(tmp_path: Path, responses):
    values: dict[str, Any] = StoreConfig.from_env().__dict__ | {
        "database_url": "postgresql://test/test",
        "blob_root": tmp_path,
    }
    connection = FakeConnection(responses)
    return (
        InspectionService(
            StoreConfig(**values),
            connection_factory=lambda: connection,
        ),
        connection,
    )


def _attempt_row(status: str, failure_class: str | None) -> tuple[Any, ...]:
    now = datetime.now(timezone.utc)
    return (
        uuid4(),
        uuid4(),
        "fr_test",
        uuid4(),
        "fc_test",
        uuid4(),
        1,
        "firecrawl_main_content",
        "v1",
        "markdown",
        now,
        now,
        status,
        200,
        "complete",
        "a" * 64,
        10,
        "text/markdown",
        "b" * 64,
        10,
        "text/markdown",
        "parser",
        failure_class,
        None,
        "acceptable",
        None,
        status == "succeeded",
        None,
        None,
        None,
        None,
        [],
        0,
        now,
    )


def test_attempt_census_classifies_success_and_failure_classes(tmp_path: Path) -> None:
    candidate = uuid4()
    run_id = uuid4()
    attempts = [
        _attempt_row("succeeded", "none"),
        _attempt_row("failed", "timeout"),
        _attempt_row("partial", None),
    ]
    inspector, connection = service(
        tmp_path,
        [
            [(run_id, "fr_test")],
            attempts,
            [
                ("succeeded", "none", 1),
                ("failed", "timeout", 1),
                ("partial", None, 1),
            ],
            [],
        ],
    )
    result = inspector.list_extraction_attempts(candidate_id=candidate)

    assert result["attempt_census"] == {
        "attempted": 3,
        "succeeded": 1,
        "unsuccessful": 2,
        "failure_counts": {"timeout": 1, "unclassified": 1},
    }
    census_statements = [
        (statement, params)
        for statement, params in connection.cursor_value.statements
        if "GROUP BY 1,2" in statement
    ]
    census_statement, census_params = census_statements[0]
    assert "LIMIT" not in census_statement
    assert census_params == (candidate,)


def test_attempt_census_is_conserved_for_success_only(tmp_path: Path) -> None:
    candidate = uuid4()
    run_id = uuid4()
    inspector, _ = service(
        tmp_path,
        [
            [(run_id, "fr_test")],
            [_attempt_row("succeeded", "none")],
            [("succeeded", "none", 1)],
            [],
        ],
    )
    result = inspector.list_extraction_attempts(candidate_id=candidate)

    census = result["attempt_census"]
    assert census["attempted"] == census["succeeded"] + census["unsuccessful"]


def test_run_rows_carry_bounded_temporal_gap_flag(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        (
            uuid4(),
            "fr_gap",
            "gap",
            "indexing",
            3,
            "agent_led",
            None,
            now,
            None,
            None,
            True,
        ),
        (
            uuid4(),
            "fr_clear",
            "clear",
            "indexing",
            3,
            "agent_led",
            None,
            now,
            None,
            None,
            False,
        ),
    ]
    inspector, connection = service(tmp_path, [rows])
    result = inspector.list_runs()

    by_external = {item["external_run_id"]: item for item in result["items"]}
    assert by_external["fr_gap"]["temporal_gap_pending"] is True
    assert by_external["fr_clear"]["temporal_gap_pending"] is False
    statement = connection.cursor_value.statements[0][0]
    assert "evidence.temporal_coverage_gap" in statement
    assert "evidence.temporal_coverage_resolved" in statement
