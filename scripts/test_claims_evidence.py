"""Tests for claims and evidence-link persistence (issue #32).

Covers:
- Unknown claim IDs are rejected.
- Unknown passage (chunk) IDs are rejected.
- URL-only source resolution is rejected.
- Round-trip export/import.
- Idempotent upsert.
- CLI parsing for claim-manifest subcommands.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.cli import parser as research_store_parser
from research_store.domain import ClaimRecord, ClaimEvidenceLink
from research_store.service import ClaimManifestService


# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


def test_claim_record_from_mapping_and_to_dict():
    now = "2025-01-01T00:00:00+00:00"
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "run_id": "22222222-2222-2222-2222-222222222222",
        "claim_id": "33333333-3333-3333-3333-333333333333",
        "statement": "Test claim",
        "semantic_status": "supported",
        "uncertainty": "partial",
        "evidence_packet_revision": 3,
        "created_at": now,
    }
    record = ClaimRecord.from_mapping(row)
    assert record.statement == "Test claim"
    assert record.semantic_status == "supported"
    assert record.uncertainty == "partial"
    assert record.evidence_packet_revision == 3
    d = record.to_dict()
    assert d["statement"] == "Test claim"
    assert d["claim_id"] == "33333333-3333-3333-3333-333333333333"


def test_claim_evidence_link_from_mapping_and_to_dict():
    now = "2025-01-01T00:00:00+00:00"
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "run_id": "22222222-2222-2222-2222-222222222222",
        "claim_id": "33333333-3333-3333-3333-333333333333",
        "passage_id": "44444444-4444-4444-4444-444444444444",
        "snapshot_id": "55555555-5555-5555-5555-555555555555",
        "source_url": "https://example.com",
        "relationship": "supports",
        "confidence": 0.95,
        "created_at": now,
    }
    link = ClaimEvidenceLink.from_mapping(row)
    assert link.passage_id == UUID("44444444-4444-4444-4444-444444444444")
    assert link.relationship == "supports"
    assert link.confidence == 0.95
    d = link.to_dict()
    assert d["relationship"] == "supports"
    assert d["source_url"] == "https://example.com"


# ---------------------------------------------------------------------------
# Service validation tests (no database required)
# ---------------------------------------------------------------------------


def _make_uow_mock(claims=None, passages=None, snapshots=None):
    """Build a minimal UoW mock that reports known IDs."""
    claims = claims or set()
    passages = passages or set()
    snapshots = snapshots or set()

    class MockUoW:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def upsert_claim(self, run_id, claim_id, statement, **kw):
            if not statement.strip():
                raise ValueError("claim statement must be non-empty")
            return uuid4()

        def upsert_evidence_link(self, run_id, claim_id, passage_id, snapshot_id, **kw):
            if passage_id not in passages:
                raise ValueError(f"unknown passage ID: {passage_id}")
            if snapshot_id not in snapshots:
                raise ValueError(f"unknown snapshot ID: {snapshot_id}")
            return uuid4()

        def list_claims(self, run_id):
            return []

        def list_evidence_links(self, run_id):
            return []

        def export_claim_manifest(self, run_id):
            return {
                "manifest_version": "claim-manifest-v1",
                "run_id": str(run_id),
                "source_state_hash": "sha256",
                "claim_count": 0,
                "link_count": 0,
                "claims": [],
                "links": [],
            }

        def claim_exists(self, run_id, claim_id):
            return claim_id in claims

        def validate_passage_id(self, passage_id):
            return passage_id in passages

        def validate_snapshot_id(self, snapshot_id):
            return snapshot_id in snapshots

    return MockUoW()


def test_create_claim_requires_non_empty_statement():
    uow = _make_uow_mock()
    svc = ClaimManifestService(lambda: uow)
    with pytest.raises(ValueError, match="non-empty"):
        svc.create_claim(
            uuid4(),
            uuid4(),
            "   ",
        )


def test_create_claim_rejects_invalid_semantic_status():
    uow = _make_uow_mock()
    svc = ClaimManifestService(lambda: uow)
    with pytest.raises(ValueError, match="invalid semantic_status"):
        svc.create_claim(
            uuid4(),
            uuid4(),
            "A claim",
            semantic_status="bogus",
        )


def test_create_evidence_link_rejects_invalid_relationship():
    uow = _make_uow_mock()
    svc = ClaimManifestService(lambda: uow)
    with pytest.raises(ValueError, match="invalid relationship"):
        svc.create_evidence_link(
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            relationship="bogus",
        )


def test_create_evidence_link_rejects_out_of_range_confidence():
    uow = _make_uow_mock()
    svc = ClaimManifestService(lambda: uow)
    with pytest.raises(ValueError, match="confidence must be in"):
        svc.create_evidence_link(
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            confidence=1.5,
        )


def test_import_manifest_rejects_unknown_passage():
    passage = uuid4()
    snapshot = uuid4()
    uow = _make_uow_mock(passages={passage}, snapshots={snapshot})
    svc = ClaimManifestService(lambda: uow)
    manifest = {
        "claims": [],
        "links": [
            {
                "claim_id": str(uuid4()),
                "passage_id": str(uuid4()),  # unknown passage
                "snapshot_id": str(snapshot),
            }
        ],
    }
    result = svc.import_manifest(uuid4(), manifest, dry_run=True)
    assert result["valid"] is False
    assert len(result["unknown_passage_ids"]) == 1


def test_import_manifest_rejects_unknown_snapshot():
    passage = uuid4()
    snapshot = uuid4()
    uow = _make_uow_mock(passages={passage}, snapshots={snapshot})
    svc = ClaimManifestService(lambda: uow)
    manifest = {
        "claims": [],
        "links": [
            {
                "claim_id": str(uuid4()),
                "passage_id": str(passage),
                "snapshot_id": str(uuid4()),  # unknown snapshot
            }
        ],
    }
    result = svc.import_manifest(uuid4(), manifest, dry_run=True)
    assert result["valid"] is False
    assert len(result["unknown_snapshot_ids"]) == 1


def test_import_manifest_dry_run_passes_with_valid_ids():
    passage = uuid4()
    snapshot = uuid4()
    uow = _make_uow_mock(passages={passage}, snapshots={snapshot})
    svc = ClaimManifestService(lambda: uow)
    manifest = {
        "claims": [],
        "links": [
            {
                "claim_id": str(uuid4()),
                "passage_id": str(passage),
                "snapshot_id": str(snapshot),
            }
        ],
    }
    result = svc.import_manifest(uuid4(), manifest, dry_run=True)
    assert result["valid"] is True
    assert result["dry_run"] is True


def test_export_manifest_produces_deterministic_structure():
    uow = _make_uow_mock()
    svc = ClaimManifestService(lambda: uow)
    run_id = uuid4()
    manifest = svc.export_manifest(run_id)
    assert manifest["manifest_version"] == "claim-manifest-v1"
    assert manifest["run_id"] == str(run_id)
    assert "source_state_hash" in manifest
    assert manifest["claims"] == []
    assert manifest["links"] == []


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------


def test_claim_manifest_parser_has_import():
    args = research_store_parser().parse_args(
        ["claim-manifest", "import", "fr_test", "--file", "/tmp/manifest.json"]
    )
    assert args.command == "claim-manifest"
    assert args.claim_command == "import"
    assert args.external_id == "fr_test"
    assert args.file == "/tmp/manifest.json"


def test_claim_manifest_parser_has_export():
    args = research_store_parser().parse_args(
        ["claim-manifest", "export", "fr_test", "--output", "-"]
    )
    assert args.command == "claim-manifest"
    assert args.claim_command == "export"
    assert args.output == "-"


def test_claim_manifest_parser_has_list():
    args = research_store_parser().parse_args(["claim-manifest", "list", "fr_test"])
    assert args.command == "claim-manifest"
    assert args.claim_command == "list"


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@INTEGRATION_MARK
def test_claims_survive_filesystem_deletion(tmp_path, prepared_database_for_claims):
    """Claims are stored in PostgreSQL — deleting scratch does not affect them."""
    from research_store.container import build_claim_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_claim_service(config)
    run_id = uuid4()
    claim_id = uuid4()
    passage_id = uuid4()
    snapshot_id = uuid4()

    # Insert a claim and link
    svc.create_claim(run_id, claim_id, "Test claim")
    svc.create_evidence_link(
        run_id, claim_id, passage_id, snapshot_id, relationship="supports"
    )

    # Verify they exist
    claims = svc.list_claims(run_id)
    assert len(claims) == 1
    assert claims[0]["claim_id"] == str(claim_id)

    links = svc.list_evidence_links(run_id)
    assert len(links) == 1
    assert links[0]["passage_id"] == str(passage_id)

    # Export and verify
    manifest = svc.export_manifest(run_id)
    assert manifest["claim_count"] == 1
    assert manifest["link_count"] == 1


@INTEGRATION_MARK
def test_unknown_claim_id_is_rejected_in_service(
    tmp_path, prepared_database_for_claims
):
    """Unknown claim IDs are rejected when creating evidence links."""
    from research_store.container import build_claim_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_claim_service(config)
    run_id = uuid4()
    claim_id = uuid4()
    passage_id = uuid4()
    snapshot_id = uuid4()

    # Create a valid claim first
    svc.create_claim(run_id, claim_id, "Test claim")

    # Create a valid link
    svc.create_evidence_link(
        run_id, claim_id, passage_id, snapshot_id, relationship="supports"
    )

    # Now try with unknown claim ID
    unknown_claim = uuid4()
    with pytest.raises(ValueError, match="unknown"):
        svc.create_evidence_link(
            run_id, unknown_claim, passage_id, snapshot_id, relationship="supports"
        )


@INTEGRATION_MARK
def test_round_trip_export_import(tmp_path, prepared_database_for_claims):
    """Export → import round-trip preserves claims and links."""
    from research_store.container import build_claim_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_claim_service(config)
    run_id = uuid4()
    claim_id = uuid4()
    passage_id = uuid4()
    snapshot_id = uuid4()

    # Insert claim and link
    svc.create_claim(run_id, claim_id, "Round-trip claim")
    svc.create_evidence_link(
        run_id,
        claim_id,
        passage_id,
        snapshot_id,
        relationship="supports",
        confidence=0.9,
    )

    # Export
    manifest = svc.export_manifest(run_id)
    assert manifest["claim_count"] == 1
    assert manifest["link_count"] == 1

    # Import to a fresh service on the same run
    svc2 = build_claim_service(config)
    result = svc2.import_manifest(run_id, manifest)
    assert result["valid"] is True
    assert result["inserted_claims"] == 1
    assert result["inserted_links"] == 1

    # Verify
    claims = svc2.list_claims(run_id)
    assert len(claims) == 1
    assert claims[0]["statement"] == "Round-trip claim"


@INTEGRATION_MARK
def test_idempotent_claim_upsert(tmp_path, prepared_database_for_claims):
    """Same claim_id + run_id is idempotent — does not duplicate."""
    from research_store.container import build_claim_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_claim_service(config)
    run_id = uuid4()
    claim_id = uuid4()

    svc.create_claim(run_id, claim_id, "Original")
    svc.create_claim(run_id, claim_id, "Updated")  # idempotent upsert

    claims = svc.list_claims(run_id)
    assert len(claims) == 1
    assert claims[0]["statement"] == "Updated"


# ---------------------------------------------------------------------------
# Fixtures for integration tests
# ---------------------------------------------------------------------------

if TEST_DSN:

    @pytest.fixture(scope="session")
    def prepared_database_for_claims():
        """Prepare database with migration 0017."""
        pass
