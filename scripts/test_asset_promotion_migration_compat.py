"""PostgreSQL integration scenarios for issue #211."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from asset_promotion_test_support import TEST_DSN, _request
from research_store.asset_promotion_service import AssetPromotionService
from research_store.config import StoreConfig
from research_store.container import build_run_service, build_service
from research_store.postgres import connect, migrate

pytest_plugins = ("asset_promotion_test_support",)


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def test_prior_head_rows_remain_unknown_without_fabricated_events(
    promotion_config: StoreConfig,
    tmp_path: Path,
):
    from psycopg import sql

    database = f"firecrawl_promotion_test_{uuid4().hex}"
    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
    try:
        assert migrate(isolated_dsn, "0039_index_checkpoint_guard") == 39
        config = replace(
            promotion_config,
            database_url=isolated_dsn,
            blob_root=tmp_path / "legacy-blobs",
            qdrant_collection=f"legacy_{uuid4().hex}",
        )
        runs = build_run_service(config)
        corpus = build_service(config)
        status = runs.create(
            "legacy promotion compatibility",
            f"fr_legacy_{uuid4().hex}",
            execution_mode="autonomous_local",
        )
        manifest = corpus.ingest_batch(
            f"fc_legacy_{uuid4().hex}",
            "scrape",
            [_request("legacy")],
            research_run_external_id=status.external_id,
        )
        assert manifest["failure_count"] == 0

        assert migrate(isolated_dsn) == 41
        service = AssetPromotionService(runs.uow_factory)
        assets = service.list_assets(status.id)
        assert len(assets) == 1
        assert assets[0]["current_stage"] == "unknown"
        assert assets[0]["provenance"] == "legacy_unstructured"
        assert service.list_events(status.id) == []

        alembic = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = isolated_dsn
        try:
            with pytest.raises(RuntimeError, match="forward-only"):
                command.downgrade(alembic, "0039_index_checkpoint_guard")
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
