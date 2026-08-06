"""PostgreSQL integration coverage for issue #212 curated run mode."""

from __future__ import annotations

from uuid import UUID, uuid4

from asset_promotion_test_support import TEST_DSN, _mark_run_index_complete
from research_store.asset_promotion_service import AssetPromotionService
from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from research_store.curated_run_service import CuratedRunService
from research_store.domain import IngestRequest
from research_store.postgres import connect

pytest_plugins = ("asset_promotion_test_support",)


def _ap_request(index: int) -> IngestRequest:
    token = uuid4().hex
    return IngestRequest(
        f"https://apnews.com/article/curated-lifecycle-{index}-{token}",
        (
            f"# AP asset {index}\n\nPostgreSQL-authoritative curated evidence {token}."
        ).encode(),
    )


def test_curated_four_ap_asset_lifecycle_is_exact_and_idempotent(
    promotion_config: StoreConfig,
) -> None:
    runs = build_run_service(promotion_config)
    workflow = build_workflow_operation_service(promotion_config)
    promotions = AssetPromotionService(runs.uow_factory)
    curated = CuratedRunService(runs, workflow, promotions)
    corpus = build_service(promotion_config)
    external_id = f"fr_curated_{uuid4().hex}"

    started = curated.start(
        "Complete a curated run using four exact AP assets",
        external_id,
        run_mode="curated",
    )
    assert started.run_mode == "curated"
    prepared = curated.prepare(external_id)
    assert prepared.run.state == "acquiring"
    acquisition_revision = prepared.run.lifecycle_revision

    search_id = f"fc_curated_search_{uuid4().hex}"
    search = workflow.begin_operation(
        external_id,
        search_id,
        "fsearch",
        {"query": "four exact AP assets"},
    )
    workflow.complete_operation(
        external_id,
        search_id,
        succeeded=True,
        output={"records": [{"persisted": True}]},
    )

    scrape_id = f"fc_curated_scrape_{uuid4().hex}"
    scrape = workflow.begin_operation(
        external_id,
        scrape_id,
        "fscrape",
        {
            "urls": [
                f"https://apnews.com/article/curated-lifecycle-{index}"
                for index in range(4)
            ]
        },
    )
    manifest = corpus.ingest_batch(
        f"fc_curated_assets_{uuid4().hex}",
        "scrape",
        [_ap_request(index) for index in range(4)],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    assert len(manifest["assets"]) == 4
    workflow.complete_operation(
        external_id,
        scrape_id,
        succeeded=True,
        output={"records": [{"persisted": True} for _ in range(4)]},
    )

    for invocation in (search, scrape):
        assert invocation.lifecycle_revision == acquisition_revision
        assert invocation.metadata["lifecycle_state"] == "acquiring"
        assert invocation.metadata["lifecycle_revision"] == acquisition_revision

    subjects = [item for item in promotions.list_assets(started.run.id) if item["id"]]
    assert len(subjects) == 4
    assert len({item["snapshot_id"] for item in subjects}) == 4
    for subject in subjects:
        retained = curated.retain(external_id, UUID(str(subject["id"])))
        assert retained["current_stage"] == "retained"

    first_seal = curated.seal_acquisition(external_id)
    second_seal = curated.seal_acquisition(external_id)
    assert first_seal == second_seal
    assert first_seal["state"] == "indexing"
    assert first_seal["expected_asset_count"] == 4
    assert first_seal["expected_chunk_count"] >= 4

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT operation,lifecycle_revision,
                      metadata->>'lifecycle_state',
                      (metadata->>'lifecycle_revision')::bigint
                 FROM research_invocations
                WHERE external_invocation_id IN (%s,%s)
                ORDER BY operation""",
            (search_id, scrape_id),
        )
        provenance = cursor.fetchall()
        assert provenance == [
            ("fscrape", acquisition_revision, "acquiring", acquisition_revision),
            ("fsearch", acquisition_revision, "acquiring", acquisition_revision),
        ]

    _mark_run_index_complete(started.run.id)
    first_finish = curated.finish(external_id, outcome="satisfied")
    second_finish = curated.finish(external_id, outcome="satisfied")
    assert first_finish.run.state == second_finish.run.state == "completed"
    resumed = curated.resume(external_id)
    assert resumed["state"] == "completed"
    assert resumed["next_action"] == "none"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
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
