"""Shared PostgreSQL integration support for issue #211."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.composition import build_run_service, build_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def promotion_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"promotion_{uuid4().hex}",
        embedding_dimension=4,
    )


def _request(label: str) -> IngestRequest:
    token = uuid4().hex
    return IngestRequest(
        f"https://promotion.example/{label}/{token}",
        f"# {label}\n\nPostgreSQL owns {token}.".encode(),
    )


def _advance_to_indexing(runs, status):
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
            idempotency_key=f"promotion-seed:{status.external_id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1
    current = runs.status(run_id=status.id)
    assert current.state == "indexing"
    return current


def _seed_retained_assets(config: StoreConfig, count: int = 1):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_promotion_{uuid4().hex}"
    status = runs.create(
        "issue 211 staged asset promotion",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc_promotion_{uuid4().hex}",
        "scrape",
        [_request(f"seed-{index}") for index in range(count)],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    assert sum(asset["status"] == "complete" for asset in manifest["assets"]) == count
    return corpus, runs, _advance_to_indexing(runs, status), manifest


def _subject_rows(run_id: UUID):
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,snapshot_id,current_stage,stage_revision,provenance
                 FROM run_asset_promotion_subjects
                WHERE run_id=%s ORDER BY snapshot_id,id""",
            (run_id,),
        )
        return cursor.fetchall()


def _subject_id_for_snapshot(run_id: UUID, snapshot_id: UUID) -> UUID:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id FROM run_asset_promotion_subjects
                WHERE run_id=%s AND snapshot_id=%s""",
            (run_id, snapshot_id),
        )
        row = cursor.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _promote(
    service: AssetPromotionService,
    subject_id: UUID,
    stage: str,
    lifecycle_revision: int,
):
    return service.promote(
        subject_id,
        stage,
        expected_lifecycle_revision=lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="test_asset_promotion_integration",
        policy_version="test-promotion-v1",
        reason_code=f"test_{stage}",
        reason=f"test transition to {stage}",
    )


def _mark_run_index_complete(run_id: UUID) -> None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs job
                  SET status='complete',completed_at=now(),error=NULL,
                      lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
                 FROM embedding_manifests manifest
                 JOIN chunks chunk ON chunk.id=manifest.chunk_id
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE job.manifest_id=manifest.id AND asset.run_id=%s""",
            (run_id,),
        )
        assert cursor.rowcount > 0
        cursor.execute(
            """UPDATE embedding_manifests manifest
                  SET index_status='complete',indexed_at=now(),error=NULL
                 FROM chunks chunk
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE manifest.chunk_id=chunk.id AND asset.run_id=%s""",
            (run_id,),
        )


def _insert_candidate(run_id: UUID, label: str) -> UUID:
    canonical_url = f"https://promotion.example/candidate/{label}/{uuid4().hex}"
    canonical_hash = hashlib.sha256(canonical_url.encode()).hexdigest()
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO search_candidates(
                   run_id,canonical_url,canonical_url_sha256,original_url,
                   domain,backend)
                 VALUES(%s,%s,%s,%s,'promotion.example','integration-test')
                 RETURNING id""",
            (run_id, canonical_url, canonical_hash, canonical_url),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("candidate insert did not return an id")
        return UUID(str(row[0]))
