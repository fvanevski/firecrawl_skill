from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.composition import build_run_service, build_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import migrate
from firecrawl_skill.research_store.retrieval.projection.index_checkpoint_service import (
    IndexCheckpointService,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def test_checkpoint_presentation_conserves_document_chunk_membership(tmp_path: Path):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue302_checkpoint_{uuid4().hex}",
        embedding_dimension=4,
    )
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_issue302_checkpoint_{uuid4().hex}"
    status = runs.create(
        "issue 302 checkpoint observability",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc_issue302_checkpoint_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://checkpoint-302.example/{uuid4().hex}",
                ("# First\n\n" + "alpha beta gamma " * 300).encode(),
            ),
            IngestRequest(
                f"https://checkpoint-302.example/{uuid4().hex}",
                ("# Second\n\n" + "delta epsilon zeta " * 300).encode(),
            ),
        ],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0

    revision = status.lifecycle_revision
    for next_state in (
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
    ):
        runs.transition(
            status.id,
            next_state,
            expected_revision=revision,
            idempotency_key=f"issue302-checkpoint:{status.id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1
    current = runs.status(run_id=status.id)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        current.id,
        lifecycle_revision=current.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(current.id),
        idempotency_key=f"issue302-checkpoint:{current.id}:ensure",
    )

    described = checkpoints.describe_checkpoint(checkpoint)
    counts = described["per_document_chunk_counts"]
    assert described["expected_chunk_count"] == checkpoint.expected_count
    assert described["complete_chunk_count"] == checkpoint.complete_count
    assert described["document_count"] == 2
    assert counts["truncated"] is False
    assert counts["returned_count"] == 2
    assert described["expected_chunk_count"] == sum(
        item["chunk_count"] for item in counts["items"]
    )
    assert described["mapped_chunk_count"] == described["expected_chunk_count"]
    assert described["chunk_count_conserved"] is True
    assert described["entity_ids"] == [str(value) for value in checkpoint.entity_ids]
