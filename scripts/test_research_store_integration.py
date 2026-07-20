"""Opt-in PostgreSQL integration tests.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database. The
suite never guesses or reuses DATABASE_URL because migrations are stateful.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from dataclasses import replace
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.container import build_service
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def service(tmp_path):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection="research_integration_test",
        embedding_dimension=4,
    )
    return build_service(config)


def test_firecrawl_result_versioning_and_transactional_index_jobs(service):
    url = f"https://integration.example/{uuid4()}"
    first = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    unchanged = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    changed = service.ingest(IngestRequest(url, b"# V2\n\nRaw changed."))
    assert unchanged.reused_snapshot and unchanged.snapshot_id == first.snapshot_id
    assert changed.snapshot_id != first.snapshot_id
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT parent_snapshot_id FROM asset_snapshots WHERE id=%s",
            (changed.snapshot_id,),
        )
        assert cursor.fetchone()[0] == first.snapshot_id
        cursor.execute(
            "SELECT count(*) FROM index_jobs WHERE entity_id=ANY(%s)",
            (list(changed.chunk_ids),),
        )
        assert cursor.fetchone()[0] == len(changed.chunk_ids)


def test_bounded_targeted_passage_retrieval(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/{uuid4()}",
            b"# Evidence\n\nCitation-ready text.",
        )
    )
    passages = service.fetch_passages(
        list(result.chunk_ids), max_tokens=100, max_passages=1
    )
    assert len(passages) == 1
    assert passages[0]["snapshot_id"] == result.snapshot_id
    assert passages[0]["source_id"] == result.source_id
