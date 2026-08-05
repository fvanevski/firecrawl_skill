#!/usr/bin/env python3
"""Restore the immutable integration suite and patch one obsolete fixture."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: restore_integration_fixture.py SOURCE TARGET")
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    original = source.read_text(encoding="utf-8")

    old_head = 'assert cursor.fetchone()[0] == "0038_postgres_authority"'
    new_head = 'assert cursor.fetchone()[0] == "0039_index_checkpoint_guard"'
    if original.count(old_head) != 1:
        raise SystemExit(
            "expected exactly one migration-head assertion, found "
            f"{original.count(old_head)}"
        )
    expected = original.replace(old_head, new_head)

    start_marker = "    def test_append_only_trigger_enforced(self):\n"
    end_marker = "\n    def test_forward_only_downgrade(self):"
    if expected.count(start_marker) != 1 or expected.count(end_marker) != 1:
        raise SystemExit("append-only test boundaries are not unique")
    start = expected.index(start_marker)
    end = expected.index(end_marker, start)
    replacement = '''    def test_append_only_trigger_enforced(self):
        """Verify structured terminal decisions remain append-only."""
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=Path("/tmp/firecrawl-terminal-append-only-test"),
            embedding_dimension=4,
        )
        runs = build_run_service(config)
        status = runs.create(
            "append-only terminal decision",
            f"fr_test_{uuid4().hex}",
            execution_mode="autonomous_local",
        )
        runs.transition(
            status.id,
            "planning",
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"append-only:{status.id}:planning",
            actor_type="integration-test",
        )
        status = runs.status(run_id=status.id)
        key = f"append-only:{status.id}:failed"
        runs.fail(
            status.id,
            expected_revision=status.lifecycle_revision,
            idempotency_key=key,
            actor_type="integration-test",
            reason="exercise terminal decision append-only trigger",
            outcome="failed",
            error="exercise terminal decision append-only trigger",
            completion={
                "reason_code": "integration_append_only_test",
                "state_census": {
                    "schema_version": "terminal-state-census-v1",
                    "available": True,
                    "counts": {"failed": 1},
                },
            },
        )

        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
                (status.id, key),
            )
            row_id = cursor.fetchone()[0]
            cursor.execute("SAVEPOINT update_sp")
            with pytest.raises(Exception, match="terminal_decisions is append-only"):
                cursor.execute(
                    "UPDATE terminal_decisions SET outcome='partial' WHERE id=%s",
                    (row_id,),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT update_sp")
            cursor.execute("SAVEPOINT delete_sp")
            with pytest.raises(Exception, match="terminal_decisions is append-only"):
                cursor.execute("DELETE FROM terminal_decisions WHERE id=%s", (row_id,))
            cursor.execute("ROLLBACK TO SAVEPOINT delete_sp")
'''
    expected = expected[:start] + replacement + expected[end:]
    target.write_text(expected, encoding="utf-8")
    actual = target.read_text(encoding="utf-8")
    if actual != expected:
        raise SystemExit("restored fixture does not match expected transformation")
    original_tests = original.count("\ndef test_") + original.count("\n    def test_")
    actual_tests = actual.count("\ndef test_") + actual.count("\n    def test_")
    if actual_tests != original_tests:
        raise SystemExit(
            f"test-function count changed: original={original_tests} actual={actual_tests}"
        )
    if len(actual.encode("utf-8")) < 156000:
        raise SystemExit("restored fixture is unexpectedly short")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
