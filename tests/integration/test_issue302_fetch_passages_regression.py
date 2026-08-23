from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_run_service,
    build_uow_factory,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def test_valid_retained_chunk_under_budget_is_returned(tmp_path: Path):
    """F7 is non-reproducing: preserve the currently correct retrieval invariant."""

    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    runs = build_run_service(config)
    status = runs.create(
        "issue 302 retained passage regression",
        f"fr_issue302_passage_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    assert status.external_id is not None

    corpus = CorpusService(
        config,
        build_uow_factory(config),
        ContentAddressedBlobStore(config.blob_root),
    )
    result = corpus.ingest(
        IngestRequest(
            f"https://issue302-passage.example/{uuid4().hex}",
            b"# Retained passage\n\nA valid retained chunk must be returned under its token budget.",
        ),
        external_run_id=status.external_id,
    )
    assert result.chunk_ids
    chunk_id = UUID(str(result.chunk_ids[0]))

    passages = corpus.fetch_passages(
        [chunk_id],
        max_tokens=2_000,
        max_passages=8,
    )

    assert len(passages) == 1
    assert passages[0]["chunk_id"] == chunk_id
    assert passages[0]["token_count"] <= 2_000
    assert "valid retained chunk" in passages[0]["text"]
