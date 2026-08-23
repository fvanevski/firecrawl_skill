"""Integration regression for run-owned snapshot BLOB verification."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import build_run_service, build_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def test_verify_includes_run_asset_snapshot_without_extraction_attempt(
    tmp_path: Path,
) -> None:
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_issue302_verify_{uuid4().hex}"
    status = runs.create(
        "issue 302 run-owned snapshot verifier regression",
        external_id,
        execution_mode="autonomous_local",
    )

    result = corpus.ingest(
        IngestRequest(
            f"https://verify-run-asset.example/{uuid4().hex}",
            b"# Run-owned snapshot\n\nThis blob is linked by research_run_assets only.",
        ),
        external_run_id=external_id,
    )

    healthy = runs.verify(status.id)
    assert healthy["status"] == "passed"
    assert healthy["total"] == 1
    assert healthy["unique_blobs"] == 1
    assert healthy["available"] == 1
    assert healthy["artifacts"][0]["source"] == "asset_snapshot"
    assert healthy["artifacts"][0]["record_id"] == str(result.snapshot_id)

    store = ContentAddressedBlobStore(config.blob_root)
    blob_path = store.path_for(result.content_sha256)
    original = blob_path.read_bytes()

    blob_path.unlink()
    missing = runs.verify(status.id)
    assert missing["status"] == "failed"
    assert missing["missing"] == 1
    assert missing["hash_mismatch"] == 0

    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(original)
    assert runs.verify(status.id)["status"] == "passed"

    blob_path.write_bytes(b"tampered")
    corrupt = runs.verify(status.id)
    assert corrupt["status"] == "failed"
    assert corrupt["missing"] == 0
    assert corrupt["hash_mismatch"] == 1
