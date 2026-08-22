"""Issue #300 AC3 concurrency regression for zero-cost replay at the hard cap."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.acquisition.models import (
    DirectScrapeRequest,
    ScrapeTransportResult,
)
from firecrawl_skill.research_store.composition import (
    build_direct_scrape_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest
from firecrawl_skill.research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _BlockingAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def scrape(self, _url, **_kwargs):
        with self.lock:
            self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return ScrapeTransportResult(raw_payload=b"# One\n\nOne extraction attempt.")


@pytest.fixture
def replay_budget_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue300_replay_budget_{uuid4().hex}",
        embedding_dimension=4,
    )


def test_concurrent_same_key_rechecks_terminal_replay_inside_budget_lock(
    replay_budget_config: StoreConfig,
) -> None:
    runs = build_run_service(replay_budget_config)
    external_id = f"fr_issue300_replay_budget_{uuid4().hex}"
    status = runs.create("issue 300 replay-safe budget", external_id)
    build_workflow_operation_service(replay_budget_config).prepare_run(external_id)
    status = runs.status(run_id=status.id)

    adapter = _BlockingAdapter()
    service = build_direct_scrape_service(
        replay_budget_config, adapter_factory=lambda: adapter
    )
    service.budget = CandidateBudget(max_exploratory_extraction_attempts=1)
    request = [DirectScrapeRequest(url="https://example.test/replay-at-cap")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            service.execute,
            status.id,
            request,
            idempotency_key="issue300:replay-at-hard-cap",
        )
        assert adapter.started.wait(timeout=5)
        second_future = pool.submit(
            service.execute,
            status.id,
            request,
            idempotency_key="issue300:replay-at-hard-cap",
        )
        adapter.release.set()
        first = first_future.result(timeout=20)
        second = second_future.result(timeout=20)

    assert adapter.calls == 1
    assert first.replayed is False
    assert second.replayed is True
    assert first.invocation_id == second.invocation_id
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM extraction_attempts WHERE run_id=%s",
            (status.id,),
        )
        assert cursor.fetchone()[0] == 1


class _FreshLineageAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def scrape(self, _url, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return ScrapeTransportResult(
                raw_payload=b"provider failure",
                returncode=1,
                stderr=b"intentional issue300 failure",
                metadata={"failure_class": "http_error"},
            )
        return ScrapeTransportResult(
            raw_payload=f"# Fresh {self.calls}\n\nAuthoritative fresh work.".encode()
        )


def test_fresh_after_failure_and_success_preserves_immutable_invocation_lineage(
    replay_budget_config: StoreConfig,
) -> None:
    external_id = f"fr_issue300_fresh_lineage_{uuid4().hex}"
    runs = build_run_service(replay_budget_config)
    status = runs.create("issue 300 fresh lineage", external_id)
    build_workflow_operation_service(replay_budget_config).prepare_run(external_id)

    adapter = _FreshLineageAdapter()
    service = build_fscrape_service(
        replay_budget_config, adapter_factory=lambda: adapter
    )
    url = "https://example.test/fresh-lineage"

    failed = service.execute(
        FScrapeRequest(urls=(url,), research_run_id=external_id)
    )
    assert failed.status == "failed"
    assert failed.to_dict()["work_mode"] == "new"

    fresh_after_failure = service.execute(
        FScrapeRequest(
            urls=(url,),
            research_run_id=external_id,
            fresh=True,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    fresh_failure_output = fresh_after_failure.to_dict()
    assert fresh_after_failure.status == "complete"
    assert fresh_failure_output["work_mode"] == "fresh"
    assert fresh_failure_output["fresh_parent_invocation_id"] == str(
        failed.batch.invocation_id
    )

    fresh_after_success = service.execute(
        FScrapeRequest(
            urls=(url,),
            research_run_id=external_id,
            fresh=True,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    fresh_success_output = fresh_after_success.to_dict()
    assert fresh_success_output["work_mode"] == "fresh"
    assert fresh_success_output["fresh_parent_invocation_id"] == str(
        fresh_after_failure.batch.invocation_id
    )
    assert adapter.calls == 3

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,parent_invocation_id,status
                 FROM research_invocations
                WHERE run_id=%s AND operation='direct_scrape'
                ORDER BY created_at,id""",
            (status.id,),
        )
        rows = cursor.fetchall()
        assert rows == [
            (failed.batch.invocation_id, None, "failed"),
            (
                fresh_after_failure.batch.invocation_id,
                failed.batch.invocation_id,
                "complete",
            ),
            (
                fresh_after_success.batch.invocation_id,
                fresh_after_failure.batch.invocation_id,
                "complete",
            ),
        ]
        cursor.execute(
            """SELECT invocation_id,payload->>'parent_invocation_id'
                 FROM research_events
                WHERE run_id=%s AND event_type='direct_scrape_fresh_executed'
                ORDER BY sequence_number""",
            (status.id,),
        )
        assert cursor.fetchall() == [
            (
                fresh_after_failure.batch.invocation_id,
                str(failed.batch.invocation_id),
            ),
            (
                fresh_after_success.batch.invocation_id,
                str(fresh_after_failure.batch.invocation_id),
            ),
        ]
