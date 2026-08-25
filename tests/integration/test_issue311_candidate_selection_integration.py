"""PostgreSQL-backed issue #311 candidate-selection replay authority."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain.models import ExecutionMode, TimeWindow
from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.smart_orchestrator import persist_planning_bundle
from firecrawl_skill.research_store.smart_search_application import (
    canonical_plan,
    deterministic_queries,
    evaluate_budget,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)


@pytest.fixture(scope="module", autouse=True)
def prepared_database() -> None:
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)


class _ThreeCandidateAdapter:
    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        **_kwargs: Any,
    ) -> SearchAdapterResult:
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        payload = {
            "results": [
                {
                    "url": "https://same.example/a",
                    "title": "Stale same-domain result",
                    "description": "deterministic candidate A must be temporally excluded",
                    "publishedDate": "2026-08-01T12:00:00Z",
                },
                {
                    "url": "https://same.example/b",
                    "title": "Current same-domain result",
                    "description": "deterministic candidate B",
                    "publishedDate": "2026-08-23T12:00:00Z",
                },
                {
                    "url": "https://different.example/c",
                    "title": "Current different-domain result",
                    "description": "deterministic candidate C",
                    "publishedDate": "2026-08-23T12:00:00Z",
                },
            ]
        }
        return SearchAdapterResult(
            raw_payload=json.dumps(payload).encode(),
            requested_at=now,
            responded_at=now,
        )


def _selection_events(run_id: Any, response_id: Any) -> list[dict[str, Any]]:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT payload FROM research_events
               WHERE run_id=%s
                 AND event_type='acquisition.candidate_selection'
                 AND idempotency_key=%s
               ORDER BY created_at,id""",
            (run_id, f"candidate-selection:{response_id}"),
        )
        rows = cursor.fetchall()
    return [row[0] if isinstance(row[0], dict) else json.loads(row[0]) for row in rows]


def test_planned_search_persists_and_replays_deterministic_candidate_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue311_selection_{uuid4().hex}",
        embedding_dimension=4,
    )
    runs = build_run_service(config)
    status = runs.create(
        "issue311 deterministic candidate selection",
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="test",
        actor_identifier="issue311",
    )
    build_workflow_operation_service(config).prepare_run(status.external_id or "")
    status = runs.status(run_id=status.id)

    spec = replace(
        conservative_research_spec(
            "issue311 deterministic candidate selection",
            "general",
        ),
        execution_mode=ExecutionMode.DETERMINISTIC_DEBUG,
        time_window=TimeWindow(
            start="2026-08-20T00:00:00+00:00",
            end="2026-08-24T23:59:59+00:00",
            description="issue311 explicit publication window",
            uncertainty="none",
        ),
    )
    queries, _ = deterministic_queries(spec.objective)
    plan = canonical_plan(
        spec,
        queries,
        run_id=status.id,
        max_queries=2,
    )
    budget = evaluate_budget(spec, status.lifecycle_revision)
    persist_planning_bundle(
        runs,
        status.id,
        spec=spec,
        budget=budget,
        plan=plan,
        run_revision=status.lifecycle_revision,
    )

    query_text = str(plan["queries"][0]["query"])
    service = build_acquisition_service(
        config,
        search_adapter=_ThreeCandidateAdapter(),
    )
    first = service.execute_search(
        status.id,
        query_text,
        limit=2,
    )

    assert first.candidate_count == 2
    assert first.search_response["temporal_admission"]["discovered"] == 3
    assert first.search_response["temporal_admission"]["admitted"] == 2
    assert first.search_response["temporal_admission"]["ineligible"] == 1
    assert first.search_response["candidate_selection"]["schema_version"] == (
        "candidate-selection-v1"
    )
    assert first.search_response["candidate_selection"]["replayed"] is False
    selected_urls = [
        str(item.get("canonical_url") or item.get("original_url"))
        for item in first.candidates
    ]
    assert selected_urls == [
        "https://same.example/b",
        "https://different.example/c",
    ]

    events = _selection_events(status.id, first.search_response_id)
    assert len(events) == 1
    persisted = events[0]
    assert persisted["decision"]["schema_version"] == "candidate-selection-v1"
    assert persisted["semantic_provenance"]["status"] == "succeeded"
    assert all("scrape" not in label for label in persisted["semantic_labels"])
    assert all("priority" not in label for label in persisted["semantic_labels"])
    assert all(
        label["candidate_id"]
        in {str(item["candidate_id"]) for item in first.candidates}
        for label in persisted["semantic_labels"]
    )

    # Selection replay is invariant to reconstruction order.
    reordered, replay_summary = service._selection_from_snapshot(
        list(reversed(first.candidates)),
        persisted,
        max_selected=2,
    )
    assert [
        str(item.get("canonical_url") or item.get("original_url")) for item in reordered
    ] == selected_urls
    assert replay_summary["replayed"] is True

    # Mutate later canonical temporal state. The response-scoped temporal and
    # selection snapshots must continue to govern replay.
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE search_candidates
                  SET published_at='1999-01-01T00:00:00+00:00'
                WHERE run_id=%s AND id=%s""",
            (status.id, first.candidates[0]["candidate_id"]),
        )

    replay = service.execute_search(
        status.id,
        query_text,
        limit=2,
    )

    assert replay.replayed is True
    assert replay.search_response_id == first.search_response_id
    assert replay.search_response["candidate_selection"]["replayed"] is True
    assert [
        str(item.get("canonical_url") or item.get("original_url"))
        for item in replay.candidates
    ] == selected_urls
    assert _selection_events(status.id, first.search_response_id) == [persisted]

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*) FROM semantic_calls
               WHERE run_id=%s AND idempotency_key=%s""",
            (status.id, f"candidate-labels:{first.search_response_id}"),
        )
        row = cursor.fetchone()
        assert row is not None
        semantic_call_count = int(row[0])
    assert semantic_call_count == 1
