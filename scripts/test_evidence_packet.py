"""Tests for deterministic EvidencePacket foundation (issue #54).

Covers:
- Exact provenance.
- Token limits enforced deterministically.
- Empty semantic groups marked unevaluated.
- Source diversity and freshness summaries.
- Revision immutability.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from budget_policy import DEFAULT_POLICY, ResourceCaps
from research_store.evidence import EvidenceService

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _make_candidate(
    candidate_id=None,
    snapshot_id=None,
    chunk_id=None,
    text="Some excerpt text",
    url="https://example.com/source1",
    date="2025-01-01T12:00:00Z",
):
    return {
        "candidate_id": str(candidate_id or uuid4()),
        "snapshot_id": str(snapshot_id or uuid4()),
        "chunk_id": str(chunk_id or uuid4()),
        "text": text,
        "url": url,
        "date": date,
    }


def test_build_evidence_packet_deterministic_ordering_and_summaries():
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    # Intentionally out of order candidates
    c1 = _make_candidate(
        snapshot_id="b0000000-0000-0000-0000-000000000000",
        url="https://a.com",
        date="2024-01-01T00:00:00Z",
    )
    c2 = _make_candidate(
        snapshot_id="a0000000-0000-0000-0000-000000000000",
        url="https://b.com",
        date="2025-01-01T00:00:00Z",
    )

    # Caps large enough to fit both
    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[c1, c2],
        retrieval_events=[],
        effective_caps=caps,
    )

    # Check ordering
    assert str(packet.passages[0].snapshot_id) == "a0000000-0000-0000-0000-000000000000"
    assert str(packet.passages[1].snapshot_id) == "b0000000-0000-0000-0000-000000000000"

    # Check summaries
    assert packet.source_diversity_summary["unique_sources"] == 2
    assert packet.source_diversity_summary["sources"] == [
        "https://a.com",
        "https://b.com",
    ]
    assert packet.freshness_summary["oldest"] == "2024-01-01T00:00:00+00:00"
    assert packet.freshness_summary["most_recent"] == "2025-01-01T00:00:00+00:00"


def test_build_evidence_packet_token_limits_enforced():
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    c1 = _make_candidate(text="Word " * 10)
    c2 = _make_candidate(text="Word " * 100)

    # c1 is ~10 tokens. c2 is ~100. Set cap to 50 to fit only c1.
    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 50,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[
            c1,
            c2,
        ],  # Note: sorted by snapshot_id inside, we can just check length
        retrieval_events=[],
        effective_caps=caps,
    )

    # Only 1 passage should be included due to limits
    assert len(packet.passages) == 1
    assert len(packet.omitted_passages) == 1

    # Near duplicate group should have the omitted candidates
    assert len(packet.near_duplicate_groups) == 1
    group = packet.near_duplicate_groups[0]
    assert group.rationale == "omitted_due_to_budget"
    assert not group.evaluated
    assert len(group.passage_ids) == 1
    assert group.passage_ids[0] == packet.omitted_passages[0].passage_id


def ensure_run_exists(dsn, run_id):
    from research_store.postgres import connect

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version, llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "running",
                "created",
                "agent_led",
                "test request",
            ),
        )

from dataclasses import replace

from research_store.config import StoreConfig
from research_store.container import build_evidence_service

@pytest.fixture(scope="session")
def prepared_database_for_evidence_packets(prepared_database_for_claims):
    # prepared_database_for_claims already upgrades to head which now includes 0029
    pass

@INTEGRATION_MARK
def test_evidence_packet_persistence_and_immutability(
    tmp_path, prepared_database_for_evidence_packets
):
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_evidence_service(config)
    run_id = uuid4()
    ensure_run_exists(TEST_DSN, run_id)
    spec_id = uuid4()

    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet1 = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=1,
        candidates=[_make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )

    # Persist first packet
    rev1 = svc.persist_packet(packet1)
    assert rev1 == 1

    # Persist second packet (simulate a new revision)
    packet2 = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=2,
        candidates=[_make_candidate(), _make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )
    rev2 = svc.persist_packet(packet2)
    assert rev2 == 2

    # Export them back
    exported1 = svc.export_packet(run_id, 1)
    assert exported1["packet_revision"] == 1
    assert exported1["coverage_revision"] == 1
    assert len(exported1["payload"]["passages"]) == 1

    exported2 = svc.export_packet(run_id, 2)
    assert exported2["packet_revision"] == 2
    assert exported2["coverage_revision"] == 2
    assert len(exported2["payload"]["passages"]) == 2

    # Export latest (should be 2)
    exported_latest = svc.export_packet(run_id)
    assert exported_latest["packet_revision"] == 2
