from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.exact_source_authority import candidate_identity_map
from firecrawl_skill.research_store.read_models import (
    CandidateOccurrenceRecord,
    CandidateRecord,
    ExtractedAssetRecord,
    candidate_record_to_public_dict,
)


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _candidate_mapping(**overrides: object) -> dict[str, object]:
    candidate_id = overrides.pop("candidate_id", uuid4())
    value: dict[str, object] = {
        "candidate_id": candidate_id,
        "run_id": uuid4(),
        "canonical_url": "https://example.com/article",
        "canonical_url_sha256": "a" * 64,
        "original_url": "https://example.com/article?utm_source=test",
        "title": "Example",
        "snippet": "Snippet",
        "domain": "example.com",
        "backend": "firecrawl",
        "published_at": NOW,
        "date_signals": {},
        "backend_metadata": {},
        "recurrence_count": 1,
        "duplicate_group_id": None,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "created_at": NOW,
        "independence_assessment": None,
    }
    value.update(overrides)
    return value


def test_candidate_mapping_requires_canonical_candidate_id() -> None:
    value = _candidate_mapping()
    legacy_id = value.pop("candidate_id")
    value["id"] = legacy_id

    with pytest.raises(ValueError, match="requires candidate_id"):
        CandidateRecord.from_mapping(value)


def test_candidate_mapping_rejects_conflicting_legacy_id() -> None:
    value = _candidate_mapping(id=uuid4())

    with pytest.raises(ValueError, match="conflicting candidate_id and legacy id"):
        CandidateRecord.from_mapping(value)


def test_candidate_mapping_accepts_matching_legacy_id_but_canonicalizes_once() -> None:
    candidate_id = uuid4()
    record = CandidateRecord.from_mapping(
        _candidate_mapping(candidate_id=candidate_id, id=candidate_id)
    )

    assert record.candidate_id == candidate_id
    public = candidate_record_to_public_dict(record)
    assert public["id"] == candidate_id
    assert "candidate_id" not in public


def test_occurrence_keeps_occurrence_and_candidate_identity_distinct() -> None:
    occurrence_id = uuid4()
    candidate_id = uuid4()
    record = CandidateOccurrenceRecord(
        occurrence_id=occurrence_id,
        candidate_id=candidate_id,
        run_id=uuid4(),
        search_response_id=uuid4(),
        plan_id=None,
        plan_query_id=None,
        rank=1,
        query_text="query",
        canonical_url="https://example.com/a",
        original_url="https://example.com/a?ref=search",
        source_url="https://example.com/a",
        final_url="https://example.com/a",
        title="A",
        snippet="B",
        raw_item={},
    )

    assert record.occurrence_id == occurrence_id
    assert record.candidate_id == candidate_id
    assert record.occurrence_id != record.candidate_id


def test_resume_asset_row_is_materialized_once_and_identity_survives_resume() -> None:
    attempt_id = uuid4()
    candidate_id = uuid4()
    snapshot_id = uuid4()
    chunk_ids = (uuid4(), uuid4())
    row = (
        attempt_id,
        candidate_id,
        snapshot_id,
        "https://example.com/requested",
        list(chunk_ids),
        "https://example.com/final",
        "https://example.com/canonical",
    )

    record = ExtractedAssetRecord.from_repository_row(row)
    resumed = record.for_resume(3)

    assert resumed.extraction_attempt_id == attempt_id
    assert resumed.candidate_id == candidate_id
    assert resumed.snapshot_id == snapshot_id
    assert resumed.chunk_ids == chunk_ids
    assert resumed.ordinal == 3
    assert resumed.resume_replay is True


def test_resume_asset_row_rejects_shape_drift_and_missing_chunks() -> None:
    with pytest.raises(ValueError, match="expected 7"):
        ExtractedAssetRecord.from_repository_row((uuid4(), uuid4()))

    with pytest.raises(ValueError, match="requires chunk_ids"):
        ExtractedAssetRecord.from_repository_row(
            (
                uuid4(),
                uuid4(),
                uuid4(),
                "https://example.com/requested",
                [],
                None,
                None,
            )
        )


def test_manifest_asset_rejects_conflicting_stable_identities() -> None:
    canonical_candidate = uuid4()
    canonical_attempt = uuid4()
    base = {
        "status": "complete",
        "requested_url": "https://example.com/requested",
        "snapshot_id": str(uuid4()),
        "chunk_ids": [str(uuid4())],
    }

    with pytest.raises(ValueError, match="conflicting extracted-asset candidate identity"):
        ExtractedAssetRecord.from_manifest_mapping(
            {**base, "candidate_id": str(uuid4())},
            candidate_id=canonical_candidate,
            extraction_attempt_id=canonical_attempt,
        )

    with pytest.raises(
        ValueError, match="conflicting extracted-asset extraction attempt identity"
    ):
        ExtractedAssetRecord.from_manifest_mapping(
            {**base, "extraction_attempt_id": str(uuid4())},
            candidate_id=canonical_candidate,
            extraction_attempt_id=canonical_attempt,
        )


def test_exact_source_identity_map_uses_canonical_typed_candidate_id() -> None:
    candidate_id = uuid4()
    record = CandidateRecord.from_mapping(
        _candidate_mapping(
            candidate_id=candidate_id,
            canonical_url="https://example.com/exact",
            original_url="https://example.com/exact?utm_campaign=test",
        )
    )

    identities = candidate_identity_map([record])

    assert set(identities) == {candidate_id}
    assert identities[candidate_id] == frozenset({"https://example.com/exact"})


def test_retained_asset_is_typed_without_fabricated_extraction_attempt() -> None:
    chunk_id = uuid4()
    snapshot_id = uuid4()
    record = ExtractedAssetRecord.retained(
        candidate_id=chunk_id,
        snapshot_id=snapshot_id,
        requested_url="https://example.com/retained",
        chunk_ids=(chunk_id,),
        ordinal=2,
    )

    assert record.extraction_attempt_id is None
    assert record.candidate_id == chunk_id
    assert record.snapshot_id == snapshot_id
    assert record.chunk_ids == (chunk_id,)
    assert record.ordinal == 2
