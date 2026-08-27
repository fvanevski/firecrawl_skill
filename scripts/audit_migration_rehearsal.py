"""Disposable forward/fresh/restore migration rehearsal for ARC-17.

The repository migrations are forward-only. "Rollback" therefore means restoring
an exact pre-upgrade PostgreSQL backup and then proving that the restored schema
can be migrated forward again; it never means weakening the forward-only Alembic
contract with a destructive downgrade.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from psycopg import sql

from firecrawl_skill.research_store.postgres import connect, migrate

PREVIOUS_REVISION = "0044_terminal_provenance_guard"
HEAD_REVISION_NUMBER = 45


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def _create_database(admin_dsn: str, name: str) -> None:
    with connect(admin_dsn) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _drop_database(admin_dsn: str, name: str) -> None:
    with connect(admin_dsn) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _revision(dsn: str) -> str:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("alembic_version is empty")
    return str(row[0])


def _run(command: list[str]) -> None:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=180
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command[:2])} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def rehearse(base_dsn: str) -> dict[str, object]:
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        raise RuntimeError("pg_dump and pg_restore are required for rollback rehearsal")
    admin_dsn = _dsn_for_database(base_dsn, "postgres")
    fresh_name = f"firecrawl_arc17_fresh_{uuid4().hex[:12]}"
    upgrade_name = f"firecrawl_arc17_upgrade_{uuid4().hex[:12]}"
    fresh_dsn = _dsn_for_database(base_dsn, fresh_name)
    upgrade_dsn = _dsn_for_database(base_dsn, upgrade_name)

    result: dict[str, object] = {
        "schema_version": "audit-migration-rehearsal-v1",
        "previous_revision": PREVIOUS_REVISION,
        "head_revision_number": HEAD_REVISION_NUMBER,
    }
    try:
        _create_database(admin_dsn, fresh_name)
        fresh_revision = migrate(fresh_dsn)
        if fresh_revision != HEAD_REVISION_NUMBER:
            raise RuntimeError(
                f"fresh migration reached {fresh_revision}, "
                f"expected {HEAD_REVISION_NUMBER}"
            )
        result["fresh_database"] = {"result": "pass", "revision": _revision(fresh_dsn)}

        _create_database(admin_dsn, upgrade_name)
        previous_number = migrate(upgrade_dsn, PREVIOUS_REVISION)
        if previous_number != 44:
            raise RuntimeError(
                f"previous migration reached {previous_number}, expected 44"
            )
        with connect(upgrade_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE arc17_restore_sentinel(value text PRIMARY KEY)"
            )
            cursor.execute(
                "INSERT INTO arc17_restore_sentinel(value) "
                "VALUES('pre-upgrade-authority')"
            )

        with tempfile.TemporaryDirectory(prefix="arc17-migration-") as directory:
            backup = Path(directory) / "pre-upgrade.dump"
            _run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--file",
                    str(backup),
                    upgrade_dsn,
                ]
            )
            backup_bytes = backup.stat().st_size
            if backup_bytes <= 0:
                raise RuntimeError("pg_dump produced an empty backup")

            upgraded = migrate(upgrade_dsn)
            if upgraded != HEAD_REVISION_NUMBER:
                raise RuntimeError(
                    f"forward migration reached {upgraded}, "
                    f"expected {HEAD_REVISION_NUMBER}"
                )
            result["forward_upgrade"] = {
                "result": "pass",
                "from_revision": PREVIOUS_REVISION,
                "to_revision": _revision(upgrade_dsn),
            }

            _drop_database(admin_dsn, upgrade_name)
            _create_database(admin_dsn, upgrade_name)
            _run(
                [
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    upgrade_dsn,
                    str(backup),
                ]
            )
            restored_revision = _revision(upgrade_dsn)
            if restored_revision != PREVIOUS_REVISION:
                raise RuntimeError(
                    f"restore revision {restored_revision!r} != {PREVIOUS_REVISION!r}"
                )
            with connect(upgrade_dsn) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT value FROM arc17_restore_sentinel")
                sentinel = cursor.fetchone()
            if sentinel != ("pre-upgrade-authority",):
                raise RuntimeError(
                    "restored sentinel does not match pre-upgrade authority"
                )
            remigrated = migrate(upgrade_dsn)
            if remigrated != HEAD_REVISION_NUMBER:
                raise RuntimeError("restored database did not migrate forward to head")
            result["restore_rollback"] = {
                "result": "pass",
                "backup_bytes": backup_bytes,
                "restored_revision": restored_revision,
                "remigrated_revision": _revision(upgrade_dsn),
                "sentinel_verified": True,
            }
        result["status"] = "pass"
        return result
    finally:
        _drop_database(admin_dsn, fresh_name)
        _drop_database(admin_dsn, upgrade_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL", ""),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.database_url:
        raise SystemExit("RESEARCH_STORE_TEST_DATABASE_URL/--database-url is required")
    result = rehearse(args.database_url)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
