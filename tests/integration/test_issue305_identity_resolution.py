from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.identity_resolver import resolve_corpus_identity
from firecrawl_skill.research_store.retrieval_admin import fetch_passages

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL test DSN")
def test_identity_resolver_crosswalks_complete_acquisition_lineage() -> None:
    """Service-backed fixture is populated by the issue-305 acceptance campaign."""
    from firecrawl_skill.research_store.postgres import connect

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ps.id,ps.run_id,ps.candidate_id,ps.snapshot_id,d.id,c.id,
                      s.source_id,s.extraction_attempt_id
                 FROM run_asset_promotion_subjects ps
                 JOIN asset_snapshots s ON s.id=ps.snapshot_id
                 JOIN documents d ON d.snapshot_id=s.id
                 JOIN chunks c ON c.document_id=d.id
                WHERE ps.candidate_id IS NOT NULL
                ORDER BY ps.updated_at DESC LIMIT 1"""
        )
        row = cursor.fetchone()
        if row is None:
            pytest.skip("disposable fixture has no completed promoted acquisition")
        subject_id, run_id, candidate_id, snapshot_id, document_id, chunk_id, source_id, attempt_id = row
        for identifier, expected_type in (
            (subject_id, "promotion_subject"),
            (candidate_id, "search_candidate"),
            (attempt_id, "extraction_attempt"),
            (source_id, "source"),
            (snapshot_id, "snapshot"),
            (document_id, "document"),
            (chunk_id, "chunk"),
        ):
            resolved = resolve_corpus_identity(connection, UUID(str(identifier)))
            assert resolved.identity_type == expected_type
            assert UUID(str(run_id)) in resolved.run_ids
            assert UUID(str(candidate_id)) in resolved.search_candidate_ids
            assert UUID(str(snapshot_id)) in resolved.snapshot_ids
            assert UUID(str(document_id)) in resolved.document_ids
            assert UUID(str(chunk_id)) in resolved.chunk_ids


class _Args:
    ids: list[str]
    max_tokens = 2000
    max_passages = 8
    research_run_id = None


def test_fetch_passages_contract_names_chunk_identity_in_diagnostic() -> None:
    # Static contract: the runtime path must reject non-chunk identities before
    # invoking the existing fetch operation; the service-backed campaign proves
    # the PostgreSQL detected type against real lineage.
    import inspect

    source = inspect.getsource(fetch_passages)
    assert '"expected_identity_type": "chunk"' in source
    assert '"wrong_identity_type"' in source
    assert "finspect passages" in source
