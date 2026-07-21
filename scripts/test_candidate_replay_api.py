from __future__ import annotations

# ruff: noqa: E402

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.container import build_run_service
from research_store.postgres import connect, migrate, require_disposable_database_reset


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database():
    """Ensure database schema is up-to-date for integration tests."""
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 11


# --- Unit Tests ---

def test_list_candidates_paginated_invalid_parameters():
    class DummyUOW:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    # We test parameter validation directly in Postgres or Service layer where applicable

    # Verify limit validation in helper logic
    with pytest.raises(ValueError):
        from research_store.postgres import PostgresUnitOfWork
        # Calling with invalid limit raises error
        uow = PostgresUnitOfWork.__new__(PostgresUnitOfWork)
        # Mock connection cursor
        class MockConn:
            def cursor(self):
                pass
        uow.connection = MockConn()
        uow.list_candidates_paginated(uuid4(), limit=0)


# --- Integration Tests (requires PostgreSQL) ---

@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_candidate_replay_and_pagination_flow(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-replay-{uuid4()}"
    run_svc.create(objective="test candidate replay API", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    # Record 3 search responses with multiple overlapping candidates to test recurrence & filtering
    resp1_data = json.dumps({
        "success": True,
        "data": [
            {"url": "https://alpha.com/p1", "title": "Alpha One", "snippet": "A" * 600},
            {"url": "https://beta.org/p1", "title": "Beta One", "snippet": "B" * 200},
            {"url": "https://gamma.net/p1", "title": "Gamma One", "snippet": "G" * 100},
        ],
    })
    resp1 = run_svc.record_search_response(
        run_id, "query one", "firecrawl", resp1_data, f"key1-{uuid4()}"
    )
    run_svc.record_response_candidates(run_id, resp1["id"])

    resp2_data = json.dumps({
        "success": True,
        "data": [
            {"url": "https://alpha.com/p1", "title": "Alpha One Updated", "snippet": "A" * 600},
            {"url": "https://delta.gov/p1", "title": "Delta One", "snippet": "D" * 150},
        ],
    })
    resp2 = run_svc.record_search_response(
        run_id, "query two", "firecrawl", resp2_data, f"key2-{uuid4()}"
    )
    run_svc.record_response_candidates(run_id, resp2["id"])

    # 1. Test Paginated List (limit=2, offset=0)
    page1 = run_svc.list_candidates_paginated(run_id, limit=2, offset=0)
    assert page1["total_count"] == 4
    assert len(page1["items"]) == 2
    assert page1["has_next"] is True
    # Most recurrent candidate (alpha.com, recurrence=2) should be first
    assert page1["items"][0]["canonical_url"] == "https://alpha.com/p1"
    assert page1["items"][0]["recurrence_count"] == 2

    # 2. Test Page 2 (limit=2, offset=2)
    page2 = run_svc.list_candidates_paginated(run_id, limit=2, offset=2)
    assert len(page2["items"]) == 2
    assert page2["has_next"] is False

    # 3. Test Stable Ordering: page1 items + page2 items == all items
    all_page = run_svc.list_candidates_paginated(run_id, limit=10, offset=0)
    combined_ids = [item["id"] for item in page1["items"]] + [item["id"] for item in page2["items"]]
    all_ids = [item["id"] for item in all_page["items"]]
    assert combined_ids == all_ids

    # 4. Test Filtering by domain
    domain_filtered = run_svc.list_candidates_paginated(run_id, domain="alpha.com")
    assert domain_filtered["total_count"] == 1
    assert domain_filtered["items"][0]["domain"] == "alpha.com"

    # 5. Test Filtering by min_recurrence
    rec_filtered = run_svc.list_candidates_paginated(run_id, min_recurrence=2)
    assert rec_filtered["total_count"] == 1
    assert rec_filtered["items"][0]["canonical_url"] == "https://alpha.com/p1"

    # 6. Test Candidate Card Bounded Construction
    cand_id = UUID(str(page1["items"][0]["id"]))
    card = run_svc.get_candidate_card(cand_id, run_id=run_id, max_snippet_length=100)
    assert card["id"] == str(cand_id)
    assert card["canonical_url"] == "https://alpha.com/p1"
    assert len(card["snippet"]) <= 104  # 100 + '...'
    assert card["snippet"].endswith("...")
    assert len(card["occurrences"]) == 2  # Appears in query one and query two

    # 7. Test Offline Triage Input Assembly
    triage = run_svc.build_triage_input(run_id, limit=10, max_snippet_length=200)
    assert triage["run_id"] == str(run_id)
    assert triage["total_count"] == 4
    assert len(triage["candidate_cards"]) == 4
    assert "generated_at" in triage

    # 8. Test Offline Candidate Replay
    replayed = run_svc.replay_candidates(run_id)
    assert replayed["total_count"] == 4
    assert len(replayed["candidate_cards"]) == 4
