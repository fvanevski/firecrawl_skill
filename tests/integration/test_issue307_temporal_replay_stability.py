"""Service-backed replay-stability regression for temporal candidate admission.

Replaying an idempotent persisted search response must reproduce the same
``acquisition.temporal_admission`` event payload even when the wall clock is
far in the future: admission is evaluated against the persisted
``search_responses.responded_at`` reference, never the wall clock.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

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

PERSISTED_RESPONSE_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)
LATER_WALL_CLOCK = PERSISTED_RESPONSE_AT + timedelta(days=730)
SPEC = {"freshness_requirements": [{"max_age_days": 30}]}
QUERY = "issue-307 replay-stability"


def _provider_item() -> dict[str, Any]:
    one_day_before = PERSISTED_RESPONSE_AT - timedelta(days=1)
    return {
        "url": "https://example.org/replay-stability",
        "title": "Replay-stability fixture",
        "description": "Candidate with explicit temporal authority.",
        "published_at": one_day_before.isoformat(),
        "updated_at": one_day_before.isoformat(),
    }


class _PinnedAdapter:
    """Deterministic search adapter with a pinned ``responded_at`` reference."""

    def __init__(self, responded_at: datetime) -> None:
        self.responded_at = responded_at

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
            raw_payload=json.dumps({"results": [_provider_item()]}).encode("utf-8"),
            requested_at=self.responded_at,
            responded_at=self.responded_at,
        )


def _admission_event_payload(run_id: Any) -> dict[str, Any] | None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT payload FROM research_events
                WHERE run_id=%s AND event_type='acquisition.temporal_admission'
                ORDER BY sequence_number DESC LIMIT 1""",
            (run_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    payload = row[0]
    return payload if isinstance(payload, dict) else json.loads(payload)


def test_replayed_search_response_admission_is_wall_clock_stable(tmp_path: Any) -> None:
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

    service = build_acquisition_service(
        config, search_adapter=_PinnedAdapter(PERSISTED_RESPONSE_AT)
    )
    first = service.execute_search(status.id, QUERY)
    first_admission = first.search_response["temporal_admission"]
    assert first_admission["eligible"] == 1
    assert first_admission["evaluated_at"] == PERSISTED_RESPONSE_AT.isoformat()
    persisted = _admission_event_payload(status.id)
    assert persisted is not None
    assert persisted["summary"] == first_admission
    assert persisted["assessments"][0]["status"] == "eligible"

    # Re-run the same idempotent search while the wall clock is far in the
    # future. The persisted admission event must stay byte-stable: with a
    # wall-clock evaluation reference the candidate would flip to ineligible
    # and the idempotency guard would reject the conflicting payload.

    class _LaterWallClock:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return LATER_WALL_CLOCK

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(candidate_temporal_policy, "datetime", _LaterWallClock)
    try:
        second = service.execute_search(status.id, QUERY)
    finally:
        monkeypatched.undo()

    second_admission = second.search_response["temporal_admission"]
    assert second_admission == first_admission
    assert _admission_event_payload(status.id)["summary"] == first_admission
    assert len(second.candidates) == 1
