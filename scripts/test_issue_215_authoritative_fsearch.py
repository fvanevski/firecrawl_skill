"""Production-seam regression tests for issue #215 candidate policy."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from candidate_ranking import CandidateBudget
from research_store.candidate_policy_service import CandidatePolicyError
from research_store.config import StoreConfig
from research_store.domain import SearchAdapterResult, utcnow
from research_store.fsearch_policy_service import (
    PolicyFSearchError,
    build_policy_fsearch_service,
)
from research_store.fsearch_service import FSearchRequest
from research_store.postgres import connect, migrate, require_disposable_database_reset

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


class _StaticSearchAdapter:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def search(self, _query_text: str, **_kwargs) -> SearchAdapterResult:
        self.calls += 1
        return SearchAdapterResult(
            raw_payload=json.dumps(
                {"success": True, "data": {"web": self.items}}
            ).encode(),
            http_status=200,
            provider_request_id=f"issue-215-{self.calls}",
            transport_error=None,
            transport_metadata={
                "test": True,
                "implicit_scrape": False,
                "attempt": 1,
            },
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


@pytest.fixture
def policy_store(tmp_path):
    if not TEST_DSN:
        pytest.skip("requires explicit disposable PostgreSQL test DSN")
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) == 43
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )


def _prepared_run(config, objective="issue 215 candidate policy"):
    from research_store.container import (
        build_run_service,
        build_workflow_operation_service,
    )

    runs = build_run_service(config)
    external_id = f"fr_{uuid4().hex}"
    status = runs.create(objective=objective, external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return runs, status, external_id


def test_fsearch_persists_selected_and_rejected_ranking_provenance(policy_store):
    runs, status, external_id = _prepared_run(policy_store)
    adapter = _StaticSearchAdapter(
        [
            {
                "url": "https://apnews.com/hub/climate-change",
                "title": "Climate hub",
                "publishedDate": "2026-08-06T12:00:00Z",
                "expected_char_count": 30_000,
            },
            {
                "url": "https://example.org/2026/08/06/specific-story",
                "title": "Specific story",
                "publishedDate": "2026-08-06T12:00:00Z",
                "expected_char_count": 15_000,
            },
        ]
    )
    service = build_policy_fsearch_service(
        policy_store,
        search_adapter_factory=lambda: adapter,
    )
    service.candidate_budget = CandidateBudget(max_generic_page_share=1.0)

    result = service.execute(
        FSearchRequest(
            "specific current event",
            external_id,
            scrape_limit=0,
            idempotency_key="issue-215-ranking",
            external_invocation_id=f"fc_{uuid4().hex}",
            tbs="qdr:w",
        )
    )
    assert result.status == "complete"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT url_type,freshness_status,expected_char_count,
                      total_score,decision,rationale
                 FROM candidate_rankings
                WHERE run_id=%s ORDER BY total_score DESC,source_rank""",
            (status.id,),
        )
        rows = cursor.fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"article", "topic_hub"}
    assert {row[1] for row in rows} == {"satisfied"}
    assert {row[2] for row in rows} == {15_000, 30_000}
    assert {row[4] for row in rows} == {"rejected"}
    assert rows[0][3] > rows[1][3]
    assert all("freshness_window_days=7" in row[5] for row in rows)
    assert len(runs.list_candidates(status.id)) == 2


def test_pre_extraction_hard_budget_blocks_before_direct_scrape(policy_store):
    _runs, status, external_id = _prepared_run(policy_store)
    adapter = _StaticSearchAdapter(
        [
            {"url": "https://example.org/a", "title": "A"},
            {"url": "https://example.org/b", "title": "B"},
        ]
    )
    service = build_policy_fsearch_service(
        policy_store,
        search_adapter_factory=lambda: adapter,
    )
    service.candidate_budget = CandidateBudget(
        max_candidates=1,
        max_generic_page_share=1.0,
    )
    direct_constructed = False

    def forbidden_direct_factory():
        nonlocal direct_constructed
        direct_constructed = True
        raise AssertionError("hard budget must fail before direct scrape construction")

    service.direct_scrape_factory = forbidden_direct_factory
    with pytest.raises(PolicyFSearchError) as caught:
        service.execute(
            FSearchRequest(
                "bounded search",
                external_id,
                scrape_limit=1,
                idempotency_key="issue-215-hard-budget",
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )
    assert caught.value.reason_code == "candidate_budget_hard_limit"
    assert direct_constructed is False
    assert caught.value.budget_decision is not None
    check_id = caught.value.budget_decision.check_id
    with pytest.raises(CandidatePolicyError, match="hard limit"):
        service.policy_service.record_override(
            status.id,
            check_id,
            "max_candidates",
            reason="attempted bypass",
            author="integration-test",
        )


def test_soft_override_is_exact_auditable_and_reusable_on_identical_retry(policy_store):
    _runs, status, external_id = _prepared_run(policy_store)
    items = [
        {"url": f"https://apnews.com/hub/topic-{index}", "title": f"Hub {index}"}
        for index in range(4)
    ]
    adapter = _StaticSearchAdapter(items)
    service = build_policy_fsearch_service(
        policy_store,
        search_adapter_factory=lambda: adapter,
    )

    request = {
        "query": "generic hub audit",
        "research_run_id": external_id,
        "scrape_limit": 0,
        "idempotency_key": "issue-215-soft-budget",
    }
    with pytest.raises(PolicyFSearchError) as first:
        service.execute(
            FSearchRequest(
                **request,
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )
    decision = first.value.budget_decision
    assert decision is not None
    assert decision.reason_code == "candidate_budget_override_required"
    assert not decision.accepted

    override_id = service.policy_service.record_override(
        status.id,
        decision.check_id,
        "max_generic_page_share",
        reason="The operator explicitly accepts these hubs for this exact retained scope.",
        author="integration-test",
    )
    assert override_id

    replay = service.execute(
        FSearchRequest(
            **request,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    assert replay.status == "complete"
    assert replay.search_replayed is True
    assert adapter.calls == 1

    checks = service.policy_service.list_checks(status.id)
    pre = [item for item in checks if item["phase"] == "pre_extraction"]
    assert len(pre) == 1
    assert pre[0]["overridden_limits"] == ["max_generic_page_share"]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM budget_override_justifications WHERE run_id=%s",
            (status.id,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM candidate_rankings WHERE run_id=%s",
            (status.id,),
        )
        assert cursor.fetchone()[0] == 8


def test_changed_candidate_scope_does_not_reuse_prior_override(policy_store):
    _runs, status, external_id = _prepared_run(policy_store)
    adapter = _StaticSearchAdapter(
        [
            {"url": "https://apnews.com/hub/one"},
            {"url": "https://apnews.com/hub/two"},
        ]
    )
    service = build_policy_fsearch_service(
        policy_store,
        search_adapter_factory=lambda: adapter,
    )
    with pytest.raises(PolicyFSearchError) as first:
        service.execute(
            FSearchRequest(
                "scope one",
                external_id,
                scrape_limit=0,
                idempotency_key="scope-one",
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )
    decision = first.value.budget_decision
    assert decision is not None
    service.policy_service.record_override(
        status.id,
        decision.check_id,
        "max_generic_page_share",
        reason="scope one only",
        author="integration-test",
    )

    adapter.items.append({"url": "https://apnews.com/hub/three"})
    with pytest.raises(PolicyFSearchError) as changed:
        service.execute(
            FSearchRequest(
                "scope two",
                external_id,
                scrape_limit=0,
                idempotency_key="scope-two",
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )
    changed_decision = changed.value.budget_decision
    assert changed_decision is not None
    assert changed_decision.check_id != decision.check_id
    assert not changed_decision.accepted
