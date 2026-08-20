"""PostgreSQL integration coverage for issue #212 curated run mode."""

from __future__ import annotations

import json
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
from asset_promotion_test_support import TEST_DSN, _mark_run_index_complete
from completion_provenance_test_support import seed_authoritative_completion_provenance
from research_store.asset_promotion_models import AssetPromotionError
from research_store.asset_promotion_service import AssetPromotionService
from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_workflow_operation_service,
)
from research_store.curated_run_service import CuratedRunService
from research_store.direct_invocation_service import DirectInvocationService
from research_store.direct_scrape_service import ScrapeTransportResult
from research_store.domain import SearchAdapterResult, utcnow
from research_store.fscrape_contract import FScrapeRequest
from research_store.fscrape_service import build_fscrape_service
from research_store.fsearch_service import FSearchRequest, build_fsearch_service
from research_store.postgres import connect

pytest_plugins = ("asset_promotion_test_support",)


def _test_dsn() -> str:
    dsn = TEST_DSN
    assert dsn is not None, "integration test requires disposable PostgreSQL DSN"
    return dsn


class _EmptySearchAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, _query_text: str, **_kwargs) -> SearchAdapterResult:
        self.calls += 1
        return SearchAdapterResult(
            raw_payload=json.dumps({"success": True, "data": {"web": []}}).encode(),
            http_status=200,
            provider_request_id="curated-empty-search",
            transport_error=None,
            transport_metadata={"test": True, "implicit_scrape": False},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


class _FourAssetScrapeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def scrape(self, url: str, **_kwargs) -> ScrapeTransportResult:
        self.calls.append(url)
        token = uuid4().hex
        return ScrapeTransportResult(
            raw_payload=(
                f"# Curated AP asset\n\nPostgreSQL-authoritative evidence {token}."
            ).encode(),
            http_status=200,
            final_url=url,
            title="Curated AP asset",
            provider_request_id=f"scrape-{token}",
            metadata={"test": True},
        )


class _PausingDirectInvocationService(DirectInvocationService):
    def __init__(self, uow_factory, locked: Event, release: Event):
        super().__init__(uow_factory)
        self.locked = locked
        self.release = release

    def _after_run_lock(self, run_id, lifecycle_state, lifecycle_revision):
        del run_id, lifecycle_state, lifecycle_revision
        self.locked.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release direct invocation lock")


def _curated(config: StoreConfig):
    runs = build_run_service(config)
    workflow = build_workflow_operation_service(config)
    promotions = AssetPromotionService(runs.uow_factory)
    return runs, workflow, promotions, CuratedRunService(runs, workflow, promotions)


def test_curated_four_asset_lifecycle_uses_real_direct_wrappers(
    promotion_config: StoreConfig,
) -> None:
    runs, _workflow, _promotions, curated = _curated(promotion_config)
    external_id = f"fr_{uuid4().hex}"

    started = curated.start(
        "Complete a curated run using four exact assets",
        external_id,
        run_mode="curated",
    )
    prepared = curated.prepare(external_id)
    assert prepared.run.state == "acquiring"
    assert runs.status(run_id=started.run.id).state == "acquiring"
    acquisition_revision = prepared.run.lifecycle_revision

    search_adapter = _EmptySearchAdapter()
    search_external_id = f"fc_{uuid4().hex}"
    search_result = build_fsearch_service(
        promotion_config,
        search_adapter_factory=lambda: search_adapter,
    ).execute(
        FSearchRequest(
            "four exact AP assets",
            external_id,
            scrape_limit=0,
            external_invocation_id=search_external_id,
        )
    )
    assert search_result.status == "empty"
    assert search_adapter.calls == 1

    scrape_adapter = _FourAssetScrapeAdapter()
    scrape_external_id = f"fc_{uuid4().hex}"
    urls = tuple(
        f"https://apnews.com/article/curated-lifecycle-{index}-{uuid4().hex}"
        for index in range(4)
    )
    scrape_result = build_fscrape_service(
        promotion_config,
        adapter_factory=lambda: scrape_adapter,
    ).execute(
        FScrapeRequest(
            urls=urls,
            research_run_id=external_id,
            external_invocation_id=scrape_external_id,
        )
    )
    assert scrape_result.status == "complete"
    assert scrape_adapter.calls == list(urls)

    with connect(_test_dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,operation,lifecycle_revision,
                      metadata->>'lifecycle_state',
                      (metadata->>'lifecycle_revision')::bigint
                 FROM research_invocations
                WHERE external_invocation_id IN (%s,%s)
                ORDER BY operation""",
            (search_external_id, scrape_external_id),
        )
        invocation_rows = cursor.fetchall()
        assert [row[1:] for row in invocation_rows] == [
            ("direct_scrape", acquisition_revision, "acquiring", acquisition_revision),
            ("fsearch", acquisition_revision, "acquiring", acquisition_revision),
        ]
        invocation_ids = [row[0] for row in invocation_rows]
        cursor.execute(
            """SELECT event_type,payload->>'lifecycle_state',
                      (payload->>'lifecycle_revision')::bigint
                 FROM research_events
                WHERE invocation_id=ANY(%s)
                  AND event_type IN ('invocation_started','direct_scrape_started')
                ORDER BY event_type""",
            (invocation_ids,),
        )
        assert cursor.fetchall() == [
            ("direct_scrape_started", "acquiring", acquisition_revision),
            ("invocation_started", "acquiring", acquisition_revision),
        ]

    first_inventory = curated.assets(external_id)
    second_inventory = curated.assets(external_id)
    assert first_inventory == second_inventory
    assert first_inventory["external_id"] == external_id
    assert first_inventory["state"] == "acquiring"
    assert first_inventory["lifecycle_revision"] == acquisition_revision
    assert first_inventory["asset_count"] == 4
    subjects = [item for item in first_inventory["assets"] if item["id"]]
    assert len(subjects) == 4
    assert len({item["id"] for item in subjects}) == 4
    assert len({item["snapshot_id"] for item in subjects}) == 4

    other_external_id = f"fr_{uuid4().hex}"
    curated.start("cross-run guard", other_external_id, run_mode="curated")
    curated.prepare(other_external_id)
    assert curated.assets(other_external_id)["asset_count"] == 0
    first_subject_id = UUID(str(subjects[0]["id"]))
    stage_before = next(
        item
        for item in curated.assets(external_id)["assets"]
        if item["id"] == str(first_subject_id)
    )["current_stage"]
    with pytest.raises(AssetPromotionError, match="not requested run"):
        curated.retain(other_external_id, first_subject_id)
    stage_after = next(
        item
        for item in curated.assets(external_id)["assets"]
        if item["id"] == str(first_subject_id)
    )["current_stage"]
    assert stage_after == stage_before

    for subject in subjects:
        retained = curated.retain(external_id, UUID(str(subject["id"])))
        assert retained["current_stage"] == "retained"

    retained_inventory = curated.assets(external_id)
    assert {item["current_stage"] for item in retained_inventory["assets"]} == {
        "retained"
    }

    first_seal = curated.seal_acquisition(external_id)
    second_seal = curated.seal_acquisition(external_id)
    assert first_seal == second_seal
    assert first_seal["state"] == "indexing"
    assert first_seal["expected_asset_count"] == 4
    assert first_seal["expected_chunk_count"] >= 4
    assert curated.resume(external_id)["next_action"] == "resume index checkpoint"

    _mark_run_index_complete(started.run.id)
    provenance = seed_authoritative_completion_provenance(
        runs.uow_factory, started.run.id
    )

    first_finish = curated.finish(external_id, outcome="satisfied")
    second_finish = curated.finish(external_id, outcome="satisfied")
    assert first_finish.run.state == second_finish.run.state == "completed"
    assert curated.resume(external_id)["next_action"] == "none"

    with connect(_test_dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT source_manifest_sha256,answer_sha256
                 FROM research_runs WHERE id=%s""",
            (started.run.id,),
        )
        assert cursor.fetchone() == (
            provenance.source_manifest_sha256,
            provenance.answer_sha256,
        )
        cursor.execute(
            """SELECT next_state,count(*)
                 FROM research_run_transitions
                WHERE run_id=%s
                GROUP BY next_state""",
            (started.run.id,),
        )
        transition_counts = dict(cursor.fetchall())
    assert transition_counts == {
        "planning": 1,
        "corpus_review": 1,
        "acquiring": 1,
        "extracting": 1,
        "indexing": 1,
        "coverage_review": 1,
        "synthesizing": 1,
        "validating": 1,
        "completed": 1,
    }


def test_direct_invocation_lock_prevents_lifecycle_interleaving(
    promotion_config: StoreConfig,
) -> None:
    runs, _workflow, _promotions, curated = _curated(promotion_config)
    external_id = f"fr_{uuid4().hex}"
    started = curated.start("locked direct invocation", external_id, run_mode="curated")
    prepared = curated.prepare(external_id)
    locked = Event()
    release = Event()
    transition_done = Event()
    errors: list[BaseException] = []
    records = []
    invocation_external_id = f"fc_{uuid4().hex}"
    service = _PausingDirectInvocationService(runs.uow_factory, locked, release)

    def begin_invocation() -> None:
        try:
            records.append(
                service.begin(
                    started.run.id,
                    invocation_external_id,
                    "fsearch",
                    {"query": "locked provenance"},
                    actor_type="wrapper",
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def transition_run() -> None:
        try:
            runs.transition(
                started.run.id,
                "extracting",
                expected_revision=prepared.run.lifecycle_revision,
                idempotency_key=f"test:concurrent-transition:{external_id}",
                actor_type="integration-test",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            transition_done.set()

    begin_thread = Thread(target=begin_invocation)
    transition_thread = Thread(target=transition_run)
    begin_thread.start()
    assert locked.wait(5)
    transition_thread.start()
    assert not transition_done.wait(0.25)
    release.set()
    begin_thread.join(5)
    transition_thread.join(5)

    assert not begin_thread.is_alive()
    assert not transition_thread.is_alive()
    assert errors == []
    assert len(records) == 1
    assert records[0].metadata["lifecycle_state"] == "acquiring"
    assert records[0].metadata["lifecycle_revision"] == prepared.run.lifecycle_revision
    assert runs.status(run_id=started.run.id).state == "extracting"
