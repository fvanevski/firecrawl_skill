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
from psycopg.errors import InvalidTextRepresentation

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.persisted_types import COVERAGE_ITEM_TYPE
from firecrawl_skill.research_domain.research import CoverageItemType
from firecrawl_skill.research_store.composition import build_run_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def _create_isolated_database(database: str) -> tuple[str, str]:
    from psycopg import sql

    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
    return admin_dsn, isolated_dsn


def _drop_isolated_database(admin_dsn: str, database: str) -> None:
    from psycopg import sql

    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )


def _coverage_item_type_values(dsn: str) -> tuple[str, ...]:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT enum_range(NULL::coverage_item_type)::text[]")
        row = cursor.fetchone()
    assert row is not None
    return tuple(row[0])


def test_coverage_item_type_registry_catches_prior_head_drift_and_upgrades():
    database = f"firecrawl_coverage_registry_upgrade_{uuid4().hex}"
    admin_dsn, isolated_dsn = _create_isolated_database(database)
    try:
        assert migrate(isolated_dsn, "0045_operator_actions") == 45
        assert (
            CoverageItemType("exact_source_requirement")
            is CoverageItemType.EXACT_SOURCE_REQUIREMENT
        )
        prior_values = _coverage_item_type_values(isolated_dsn)
        assert prior_values == COVERAGE_ITEM_TYPE.persisted_values(1)
        with (
            connect(isolated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(InvalidTextRepresentation),
        ):
            cursor.execute(
                "SELECT %s::coverage_item_type", ("exact_source_requirement",)
            )

        delta = COVERAGE_ITEM_TYPE.postgres_delta(prior_values, target_version=2)
        assert [item.persisted_value for item in delta] == [
            "exact_source_requirement"
        ]
        assert migrate(isolated_dsn) == 47
        assert _coverage_item_type_values(isolated_dsn) == (
            COVERAGE_ITEM_TYPE.persisted_values()
        )
    finally:
        _drop_isolated_database(admin_dsn, database)


def test_coverage_item_type_registry_matches_fresh_head_database():
    database = f"firecrawl_coverage_registry_fresh_{uuid4().hex}"
    admin_dsn, isolated_dsn = _create_isolated_database(database)
    try:
        assert migrate(isolated_dsn) == 47
        assert _coverage_item_type_values(isolated_dsn) == (
            COVERAGE_ITEM_TYPE.persisted_values()
        )
    finally:
        _drop_isolated_database(admin_dsn, database)


def test_migration_adds_relational_append_only_policy_without_inferred_history(
    tmp_path,
):
    from psycopg import sql

    database = f"firecrawl_candidate_policy_test_{uuid4().hex}"
    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
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

        assert migrate(isolated_dsn) == 47
        with connect(isolated_dsn) as connection, connection.cursor() as cursor:
            for table in (
                "candidate_rankings",
                "corpus_budget_checks",
                "budget_override_justifications",
            ):
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                )
                row0 = cursor.fetchone()
                assert row0 is not None
                assert row0[0] == 0

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

        alembic = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
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
