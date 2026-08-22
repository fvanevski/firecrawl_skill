"""PostgreSQL integration coverage for issue #297 completion-admission preview.

The curated ``seal-acquisition`` gate runs an append-only ``completion_admission``
preview while the run is still ``acquiring``. Hard-limit and un-overridden
soft-limit failures keep the run in ``acquiring`` so the operator can re-curate
(or, for a soft limit, bind an override to the persisted preview check). An
accepted preview is remeasured under the same run lock and PostgreSQL transaction
that commits ``acquiring -> extracting``. The authoritative post-transition
check remains independent, and exact predecessor previews are recoverable after
an interrupted membership seal.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from asset_promotion_test_support import TEST_DSN

from firecrawl_skill.research_store.acquisition.candidate_ranking import (
    CandidateBudget,
    classify_url,
    is_generic_url_type,
)
from firecrawl_skill.research_store.acquisition.models import ScrapeTransportResult
from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.composition import (
    build_fscrape_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.curated_run_service import (
    CuratedRunError,
    CuratedRunService,
)
from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest
from firecrawl_skill.research_store.postgres import connect

pytest_plugins = ("asset_promotion_test_support",)
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _test_dsn() -> str:
    dsn = TEST_DSN
    assert dsn is not None, "integration test requires disposable PostgreSQL DSN"
    return dsn


class _ScrapeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def scrape(self, url: str, **_kwargs) -> ScrapeTransportResult:
        self.calls.append(url)
        token = uuid4().hex
        return ScrapeTransportResult(
            raw_payload=(
                f"# Curated 297 asset\n\nPostgreSQL-authoritative evidence {token}."
            ).encode(),
            http_status=200,
            final_url=url,
            title="Curated 297 asset",
            provider_request_id=f"scrape-{token}",
            metadata={"test": True},
        )


def _curated_with_budget(config: StoreConfig, budget: CandidateBudget):
    runs = build_run_service(config)
    workflow = build_workflow_operation_service(config)
    promotions = AssetPromotionService(runs.uow_factory, candidate_budget=budget)
    return runs, promotions, CuratedRunService(runs, workflow, promotions)


def _drive_to_retained(
    config: StoreConfig, budget: CandidateBudget, urls: tuple[str, ...]
):
    runs, promotions, curated = _curated_with_budget(config, budget)
    external_id = f"fr_{uuid4().hex}"
    curated.start("issue 297 completion admission", external_id, run_mode="curated")
    prepared = curated.prepare(external_id)
    assert prepared.run.state == "acquiring"
    run_id = prepared.run.id
    acquiring_revision = prepared.run.lifecycle_revision

    adapter = _ScrapeAdapter()
    scrape_result = build_fscrape_service(
        config, adapter_factory=lambda: adapter
    ).execute(
        FScrapeRequest(
            urls=urls,
            research_run_id=external_id,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    assert scrape_result.status == "complete"
    assert len(adapter.calls) == len(urls)

    subjects = [item for item in curated.assets(external_id)["assets"] if item["id"]]
    assert len(subjects) == len(urls)
    for subject in subjects:
        assert curated.retain(external_id, UUID(str(subject["id"])))[
            "current_stage"
        ] == ("retained")
    return runs, promotions, curated, external_id, run_id, acquiring_revision


def _subject_urls(run_id: UUID) -> dict[str, str]:
    """Map subject id to the URL the budget gate classifies (candidate, else source)."""
    with connect(_test_dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT subject.id, COALESCE(candidate.canonical_url, source.canonical_url)
                 FROM run_asset_promotion_subjects subject
                 JOIN asset_snapshots snapshot ON snapshot.id=subject.snapshot_id
                 JOIN sources source ON source.id=snapshot.source_id
                 LEFT JOIN search_candidates candidate ON candidate.id=subject.candidate_id
                WHERE subject.run_id=%s""",
            (run_id,),
        )
        return {str(row[0]): str(row[1]) for row in cursor.fetchall()}


def _completion_checks(promotions, run_id: UUID) -> list[dict[str, Any]]:
    return [
        item
        for item in promotions.candidate_policy_service.list_checks(run_id)
        if item["phase"] == "completion_admission"
    ]


def test_soft_preview_failure_stays_acquiring_until_re_curation(
    promotion_config: StoreConfig,
) -> None:
    urls = (
        f"https://apnews.com/article/curated-297-{uuid4().hex}",
        "https://example.com/",
        "https://example.com/topics/ai/",
        "https://en.wikipedia.org/wiki/Topic",
    )
    runs, promotions, curated, external_id, run_id, _revision = _drive_to_retained(
        promotion_config, CandidateBudget(), urls
    )
    url_by_subject = _subject_urls(run_id)
    generic_ids = [
        subject_id
        for subject_id, url in url_by_subject.items()
        if is_generic_url_type(classify_url(url))
    ]
    article_ids = [
        subject_id
        for subject_id, url in url_by_subject.items()
        if not is_generic_url_type(classify_url(url))
    ]
    assert len(generic_ids) == 3
    assert len(article_ids) == 1

    with pytest.raises(CuratedRunError, match="override required"):
        curated.seal_acquisition(external_id)
    assert runs.status(run_id=run_id).state == "acquiring"
    assert promotions.get_active_seal(run_id) is None

    for subject_id in generic_ids:
        curated.reject(external_id, UUID(subject_id), reason="generic page dropped")

    seal = curated.seal_acquisition(external_id)
    assert seal["state"] == "indexing"
    assert seal["expected_asset_count"] == 1
    assert runs.status(run_id=run_id).state == "indexing"
    assert promotions.get_active_seal(run_id) is not None


def test_hard_preview_failure_stays_acquiring(promotion_config: StoreConfig) -> None:
    urls = tuple(
        f"https://apnews.com/article/hard-297-{i}-{uuid4().hex}" for i in range(4)
    )
    runs, promotions, curated, external_id, run_id, _revision = _drive_to_retained(
        promotion_config, CandidateBudget(max_candidates=2), urls
    )

    with pytest.raises(CuratedRunError, match="hard limit rejected"):
        curated.seal_acquisition(external_id)

    assert runs.status(run_id=run_id).state == "acquiring"
    assert promotions.get_active_seal(run_id) is None
    assert _completion_checks(promotions, run_id) == [] or all(
        not item["accepted_without_override"]
        for item in _completion_checks(promotions, run_id)
    )


def test_accepted_preview_is_revalidated_before_first_transition(
    promotion_config: StoreConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = tuple(
        f"https://apnews.com/article/race-297-{i}-{uuid4().hex}" for i in range(2)
    )
    runs, promotions, curated, external_id, run_id, acquiring_revision = (
        _drive_to_retained(promotion_config, CandidateBudget(), urls)
    )
    retained = [
        item
        for item in curated.assets(external_id)["assets"]
        if item["current_stage"] == "retained"
    ]
    victim = UUID(str(retained[0]["id"]))
    policy = promotions.candidate_policy_service
    original = policy.evaluate_completion_admission_preview

    def _preview_then_curate(
        preview_run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
    ):
        decision = original(preview_run_id, lifecycle_revision, budget)
        assert decision.accepted
        curated.reject(
            external_id,
            victim,
            reason="simulate curation after accepted preview",
        )
        return decision

    monkeypatch.setattr(
        policy, "evaluate_completion_admission_preview", _preview_then_curate
    )

    with pytest.raises(CuratedRunError, match="changed before acquisition seal"):
        curated.seal_acquisition(external_id)

    current = runs.status(run_id=run_id)
    assert current.state == "acquiring"
    assert current.lifecycle_revision == acquiring_revision
    assert promotions.get_active_seal(run_id) is None
    assert (
        curated.reject(
            external_id,
            UUID(str(retained[1]["id"])),
            reason="retain/reject remains legal after rejected locked preview",
        )["current_stage"]
        == "rejected"
    )


def test_soft_override_rebinds_onto_authoritative_check(
    promotion_config: StoreConfig,
) -> None:
    urls = (f"https://apnews.com/article/override-297-{uuid4().hex}",)
    budget = CandidateBudget(
        max_per_asset_contribution_chunks=0, max_generic_page_share=1.0
    )
    runs, promotions, curated, external_id, run_id, acquiring_revision = (
        _drive_to_retained(promotion_config, budget, urls)
    )
    policy = promotions.candidate_policy_service

    with pytest.raises(CuratedRunError, match="override required"):
        curated.seal_acquisition(external_id)

    preview = next(
        item
        for item in _completion_checks(promotions, run_id)
        if int(item["lifecycle_revision"]) == int(acquiring_revision)
    )
    policy.record_override(
        run_id,
        UUID(str(preview["id"])),
        "max_per_asset_contribution_chunks",
        reason="single curated asset expected to contribute many chunks",
        author="operator-297",
    )

    seal = curated.seal_acquisition(external_id)
    assert seal["state"] == "indexing"

    indexed_checks = [
        item
        for item in _completion_checks(promotions, run_id)
        if int(item["lifecycle_revision"]) > int(acquiring_revision)
    ]
    assert len(indexed_checks) == 1
    assert "max_per_asset_contribution_chunks" in indexed_checks[0]["overridden_limits"]
    assert int(indexed_checks[0]["lifecycle_revision"]) == acquiring_revision + 2
    assert runs.status(run_id=run_id).state == "indexing"


def test_interrupted_soft_override_recovers_exact_predecessor_preview(
    promotion_config: StoreConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = (f"https://apnews.com/article/recover-297-{uuid4().hex}",)
    budget = CandidateBudget(
        max_per_asset_contribution_chunks=0, max_generic_page_share=1.0
    )
    runs, promotions, curated, external_id, run_id, acquiring_revision = (
        _drive_to_retained(promotion_config, budget, urls)
    )
    policy = promotions.candidate_policy_service

    with pytest.raises(CuratedRunError, match="override required"):
        curated.seal_acquisition(external_id)
    preview = next(
        item
        for item in _completion_checks(promotions, run_id)
        if int(item["lifecycle_revision"]) == acquiring_revision
    )
    policy.record_override(
        run_id,
        UUID(str(preview["id"])),
        "max_per_asset_contribution_chunks",
        reason="curated single-asset recovery test",
        author="operator-297-recovery",
    )

    original_after_step = promotions._after_promotion_step
    failed = False

    def _fail_once(step: tuple[UUID, str]) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError(f"injected issue297 interruption after {step}")
        original_after_step(step)

    monkeypatch.setattr(promotions, "_after_promotion_step", _fail_once)
    with pytest.raises(RuntimeError, match="injected issue297 interruption"):
        curated.seal_acquisition(external_id)
    assert runs.status(run_id=run_id).state == "indexing"
    assert promotions.get_active_seal(run_id) is None

    monkeypatch.setattr(promotions, "_after_promotion_step", original_after_step)
    repaired = curated.seal_acquisition(external_id)
    assert repaired["state"] == "indexing"
    assert promotions.get_active_seal(run_id) is not None
    checks = _completion_checks(promotions, run_id)
    indexed = [
        item
        for item in checks
        if int(item["lifecycle_revision"]) == acquiring_revision + 2
    ]
    assert len(indexed) == 1
    assert "max_per_asset_contribution_chunks" in indexed[0]["overridden_limits"]


def test_interrupted_between_seal_transitions_recovers_soft_override(
    promotion_config: StoreConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = (f"https://apnews.com/article/extracting-recover-297-{uuid4().hex}",)
    budget = CandidateBudget(
        max_per_asset_contribution_chunks=0, max_generic_page_share=1.0
    )
    runs, promotions, curated, external_id, run_id, acquiring_revision = (
        _drive_to_retained(promotion_config, budget, urls)
    )
    policy = promotions.candidate_policy_service

    with pytest.raises(CuratedRunError, match="override required"):
        curated.seal_acquisition(external_id)
    preview = next(
        item
        for item in _completion_checks(promotions, run_id)
        if int(item["lifecycle_revision"]) == acquiring_revision
    )
    policy.record_override(
        run_id,
        UUID(str(preview["id"])),
        "max_per_asset_contribution_chunks",
        reason="curated extracting-state recovery test",
        author="operator-297-extracting-recovery",
    )

    workflow = curated.workflow_service
    original_seal = workflow.seal_acquisition
    interrupted = False

    def _interrupt_once(
        requested_external_id: str,
        *,
        idempotency_key: str | None = None,
    ):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("injected interruption before extracting-to-indexing")
        return original_seal(
            requested_external_id,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(workflow, "seal_acquisition", _interrupt_once)
    with pytest.raises(RuntimeError, match="before extracting-to-indexing"):
        curated.seal_acquisition(external_id)

    intermediate = runs.status(run_id=run_id)
    assert intermediate.state == "extracting"
    assert intermediate.lifecycle_revision == acquiring_revision + 1
    assert promotions.get_active_seal(run_id) is None

    repaired = curated.seal_acquisition(external_id)
    assert repaired["state"] == "indexing"
    assert runs.status(run_id=run_id).lifecycle_revision == acquiring_revision + 2
    assert promotions.get_active_seal(run_id) is not None

    indexed = [
        item
        for item in _completion_checks(promotions, run_id)
        if int(item["lifecycle_revision"]) == acquiring_revision + 2
    ]
    assert len(indexed) == 1
    assert "max_per_asset_contribution_chunks" in indexed[0]["overridden_limits"]


def test_reseal_is_idempotent_and_creates_no_extra_admission_rows(
    promotion_config: StoreConfig,
) -> None:
    urls = tuple(
        f"https://apnews.com/article/idem-297-{i}-{uuid4().hex}" for i in range(2)
    )
    runs, promotions, curated, external_id, _run_id, acquiring_revision = (
        _drive_to_retained(promotion_config, CandidateBudget(), urls)
    )

    first_seal = curated.seal_acquisition(external_id)
    second_seal = curated.seal_acquisition(external_id)
    assert first_seal == second_seal
    assert first_seal["state"] == "indexing"

    completion_checks = _completion_checks(promotions, first_seal["run_id"])
    assert {int(item["lifecycle_revision"]) for item in completion_checks} == {
        int(acquiring_revision),
        int(acquiring_revision) + 2,
    }
    assert len(completion_checks) == 2
    assert runs.status(external_id=external_id).state == "indexing"
