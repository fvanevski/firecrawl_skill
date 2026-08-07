"""Migration and compatibility contract for issue #215."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.container import build_run_service
from research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def test_migration_adds_relational_append_only_policy_without_inferred_history(tmp_path):
    from psycopg import sql

    database = f"firecrawl_candidate_policy_test_{uuid4().hex}"
    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        assert migrate(isolated_dsn, "0041_search_provenance") == 41
        config = replace(
            StoreConfig.from_env(),
            database_url=isolated_dsn,
            blob_root=tmp_path / "candidate-policy-blobs",
        )
        runs = build_run_service(config)
        status = runs.create(
            "pre-0042 candidate history",
            f"fr_{uuid4().hex}",
            execution_mode="autonomous_local",
        )
        with connect(isolated_dsn) as connection, connection.cursor() as cursor:
            url = "https://example.org/historical-candidate"
            cursor.execute(
                """INSERT INTO search_candidates(
                       run_id,canonical_url,canonical_url_sha256,original_url,
                       domain,backend)
                     VALUES(%s,%s,%s,%s,'example.org','historical-test')""",
                (status.id, url, hashlib.sha256(url.encode()).hexdigest(), url),
            )

        assert migrate(isolated_dsn) == 42
        with connect(isolated_dsn) as connection, connection.cursor() as cursor:
            for table in (
                "candidate_rankings",
                "corpus_budget_checks",
                "budget_override_justifications",
            ):
                cursor.execute(f"SELECT count(*) FROM {table}")
                assert cursor.fetchone()[0] == 0

            cursor.execute(
                """SELECT conname
                     FROM pg_constraint
                    WHERE conrelid='candidate_rankings'::regclass
                      AND contype='f'"""
            )
            ranking_fks = {row[0] for row in cursor.fetchall()}
            assert len(ranking_fks) >= 4

            cursor.execute(
                """SELECT tgname FROM pg_trigger
                    WHERE tgrelid IN (
                      'candidate_rankings'::regclass,
                      'corpus_budget_checks'::regclass,
                      'budget_override_justifications'::regclass
                    ) AND NOT tgisinternal"""
            )
            triggers = {row[0] for row in cursor.fetchall()}
            assert "candidate_rankings_append_only_trigger" in triggers
            assert "corpus_budget_checks_append_only_trigger" in triggers
            assert "budget_override_justifications_append_only_trigger" in triggers

        alembic = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = isolated_dsn
        try:
            with pytest.raises(RuntimeError, match="forward-only"):
                command.downgrade(alembic, "0041_search_provenance")
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
    finally:
        with connect(admin_dsn) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
