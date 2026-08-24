"""Service-backed replay-stability regression for temporal candidate admission.

The first persisted temporal-admission event is response-scoped authority. A
later search response may legitimately update the canonical candidate row, but
replaying the older idempotent response must retain its original response clock,
assessment, candidate membership, and event payload.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store import candidate_temporal_policy
from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult
from firecrawl_skill.research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)

FIRST_RESPONSE_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)
SECOND_RESPONSE_AT = FIRST_RESPONSE_AT + timedelta(days=10)
LATER_WALL_CLOCK = FIRST_RESPONSE_AT + timedelta(days=730)
SPEC = {"freshness_requirements": [{"max_age_days": 30}]}
FIRST_QUERY = "issue-307 replay-stability first response"
SECOND_QUERY = "issue-307 replay-stability later response"


def _provider_item(authority_at: datetime) -> dict[str, Any]:
    return {
        "url": "https://example.org/replay-stability",
        "title": "Replay-stability fixture",
        "description": "Candidate with explicit temporal authority.",
        "published_at": authority_at.isoformat(),
        "updated_at": authority_at.isoformat(),
    }


class _PinnedAdapter:
    """Deterministic search adapter with pinned response and authority clocks."""

    def __init__(self, responded_at: datetime, authority_at: datetime) -> None:
        self.responded_at = responded_at
        self.authority_at = authority_at

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        **kwargs: Any,
    ) -> SearchAdapterResult:
        return SearchAdapterResult(
            raw_payload=json.dumps(
                {"results": [_provider_item(self.authority_at)]}
            ).encode("utf-8"),
            requested_at=self.responded_at,
            responded_at=self.responded_at,
        )


def _admission_event_payload(
    run_id: Any, search_response_id: UUID
) -> dict[str, Any] | None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT payload FROM research_events
                WHERE run_id=%s AND event_type='acquisition.temporal_admission'
                  AND idempotency_key=%s""",
            (run_id, f"temporal-admission:{search_response_id}"),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    payload = row[0]
    return payload if isinstance(payload, dict) else json.loads(payload)


def _candidate_publication(candidate_id: UUID) -> datetime | None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT published_at FROM search_candidates WHERE id=%s",
            (candidate_id,),
        )
        row = cursor.fetchone()
    return None if row is None else row[0]


def test_replayed_response_survives_later_canonical_candidate_mutation(
    tmp_path: Any,
) -> None:
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue307_temporal_{uuid4().hex}",
        embedding_dimension=4,
    )
    migrate(TEST_DSN)
    runs = build_run_service(config)
    external_id = f"fr_issue307_replay_{uuid4().hex}"
    runs.create("issue 307 replay stability", external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    status = runs.status(external_id=external_id)
    runs.record_research_spec(status.id, SPEC)

    first_authority = FIRST_RESPONSE_AT - timedelta(days=1)
    first_service = build_acquisition_service(
        config,
        search_adapter=_PinnedAdapter(FIRST_RESPONSE_AT, first_authority),
    )
    first = first_service.execute_search(status.id, FIRST_QUERY)
    first_admission = first.search_response["temporal_admission"]
    assert first_admission["eligible"] == 1
    assert first_admission["evaluated_at"] == FIRST_RESPONSE_AT.isoformat()
    candidate_id = UUID(str(first.candidates[0]["candidate_id"]))
    persisted_first = _admission_event_payload(status.id, first.search_response_id)
    assert persisted_first is not None
    assert persisted_first["summary"] == first_admission
    assert persisted_first["assessments"][0]["status"] == "eligible"

    later_authority = SECOND_RESPONSE_AT - timedelta(days=1)
    second_service = build_acquisition_service(
        config,
        search_adapter=_PinnedAdapter(SECOND_RESPONSE_AT, later_authority),
    )
    second = second_service.execute_search(status.id, SECOND_QUERY)
    assert UUID(str(second.candidates[0]["candidate_id"])) == candidate_id
    assert _candidate_publication(candidate_id) == later_authority
    assert later_authority > FIRST_RESPONSE_AT

    class _LaterWallClock:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return LATER_WALL_CLOCK

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(candidate_temporal_policy, "datetime", _LaterWallClock)
    try:
        replay = first_service.execute_search(status.id, FIRST_QUERY)
    finally:
        monkeypatched.undo()

    assert replay.replayed is True
    assert replay.search_response_id == first.search_response_id
    assert replay.search_response["temporal_admission"] == first_admission
    assert len(replay.candidates) == 1
    assert replay.candidates[0]["temporal_assessment"]["status"] == "eligible"
    persisted_replay = _admission_event_payload(status.id, first.search_response_id)
    assert persisted_replay == persisted_first
