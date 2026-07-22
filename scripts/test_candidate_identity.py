from __future__ import annotations

from dataclasses import replace
import hashlib
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
from research_store.url import canonicalize_candidate_url, redact_sensitive_url


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database():
    """Prove fresh and populated prior-head migrations without data loss."""
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

def test_canonicalize_candidate_url_sensitive_redaction():
    url1 = "HTTPS://API.Example.com:443/docs/page/?apiKey=secret123&utm_source=google&q=python#section"
    canonical1, orig1 = canonicalize_candidate_url(url1)
    assert canonical1 == "https://api.example.com/docs/page?q=python"
    assert orig1 == "https://API.Example.com:443/docs/page/?apiKey=[REDACTED]&utm_source=google&q=python#section"


    url2 = "http://example.org:80/path/?access_token=token456&gclid=789&view=full"
    canonical2, orig2 = canonicalize_candidate_url(url2)
    assert canonical2 == "http://example.org/path?view=full"
    assert "access_token=[REDACTED]" in orig2
    assert "gclid=789" in orig2


def test_redact_sensitive_url():
    url = "https://example.com/api?secret_key=xyz&session_id=12345&query=test"
    redacted = redact_sensitive_url(url)
    assert "secret_key=[REDACTED]" in redacted
    assert "session_id=[REDACTED]" in redacted
    assert "query=test" in redacted


# --- Integration Tests (requires PostgreSQL) ---

@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_record_response_candidates_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-cand-{uuid4()}"
    run_svc.create(objective="test candidate identity", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    raw_payload = json.dumps(
        {
            "success": True,
            "data": [
                {
                    "url": "https://example.com/item1?utm_source=test&apiKey=secret1",
                    "title": "Item One",
                    "description": "First candidate item",
                    "published_at": "2026-01-15T12:00:00Z",
                },
                {
                    "url": "https://example.com/item2",
                    "title": "Item Two",
                    "description": "Second candidate item",
                },
            ],
        }
    )

    resp = run_svc.record_search_response(
        run_id,
        query_text="candidate test query",
        backend="firecrawl",
        raw_payload=raw_payload,
        idempotency_key=f"idemp-cand-{uuid4()}",
    )
    resp_id = resp["id"]

    occs = run_svc.record_response_candidates(run_id, resp_id)
    assert len(occs) == 2

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2

    cand1 = run_svc.get_candidate(occs[0]["candidate_id"])
    assert cand1["canonical_url"] == "https://example.com/item1"
    assert cand1["original_url"] == "https://example.com/item1?utm_source=test&apiKey=[REDACTED]"
    assert cand1["title"] == "Item One"
    assert cand1["recurrence_count"] == 1
    assert cand1["domain"] == "example.com"


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_cross_branch_recurrence_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-recurrence-{uuid4()}"
    run_svc.create(objective="test cross branch recurrence", external_id=ext_id)
    run_id = run_svc.status(external_id=ext_id).id

    target_url = "https://recurrence.org/article?id=42"
    payload1 = json.dumps({"success": True, "data": [{"url": target_url, "title": "Branch 1 Title"}]})
    payload2 = json.dumps({"success": True, "data": [{"url": target_url + "&token=sec", "title": "Branch 2 Title"}]})

    resp1 = run_svc.record_search_response(
        run_id,
        query_text="query branch alpha",
        backend="firecrawl",
        raw_payload=payload1,
        idempotency_key=f"idemp-rec-1-{uuid4()}",
    )
    resp2 = run_svc.record_search_response(
        run_id,
        query_text="query branch beta",
        backend="firecrawl",
        raw_payload=payload2,
        idempotency_key=f"idemp-rec-2-{uuid4()}",
    )

    run_svc.record_response_candidates(run_id, resp1["id"])
    run_svc.record_response_candidates(run_id, resp2["id"])

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 1
    cand = cands[0]
    assert cand["canonical_url"] == "https://recurrence.org/article?id=42"
    assert cand["recurrence_count"] == 2

    occs = run_svc.list_candidate_occurrences(cand["id"])
    assert len(occs) == 2
    assert {occ["query_text"] for occ in occs} == {"query branch alpha", "query branch beta"}


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_duplicate_group_assignment_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-dup-group-{uuid4()}"
    run_svc.create(objective="test duplicate groups", external_id=ext_id)
    run_id = run_svc.status(external_id=ext_id).id

    payload = json.dumps(
        {
            "success": True,
            "data": [
                {"url": "https://example.org/doc-v1", "title": "Doc V1"},
                {"url": "https://example.org/doc-v2", "title": "Doc V2"},
            ],
        }
    )

    resp = run_svc.record_search_response(
        run_id,
        query_text="dup group query",
        backend="firecrawl",
        raw_payload=payload,
        idempotency_key=f"idemp-dup-{uuid4()}",
    )
    run_svc.record_response_candidates(run_id, resp["id"])

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2

    cid1, cid2 = cands[0]["id"], cands[1]["id"]
    group_id = run_svc.assign_duplicate_group([cid1, cid2], run_id=run_id)

    grouped_cands = run_svc.list_candidates(run_id, duplicate_group_id=group_id)
    assert len(grouped_cands) == 2
    assert {c["id"] for c in grouped_cands} == {cid1, cid2}


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_candidate_identity_separate_from_sources_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-separate-{uuid4()}"
    run_svc.create(objective="test separate candidate identity", external_id=ext_id)
    run_id = run_svc.status(external_id=ext_id).id

    payload = json.dumps({"success": True, "data": [{"url": "https://unscraped-candidate.com"}]})
    resp = run_svc.record_search_response(
        run_id,
        query_text="unscraped query",
        backend="firecrawl",
        raw_payload=payload,
        idempotency_key=f"idemp-sep-{uuid4()}",
    )
    run_svc.record_response_candidates(run_id, resp["id"])

    # Search candidates table has candidate
    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 1

    # Sources table MUST NOT have any row created for this unscraped candidate
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sources WHERE canonical_url='https://unscraped-candidate.com'")
        assert cur.fetchone()[0] == 0
