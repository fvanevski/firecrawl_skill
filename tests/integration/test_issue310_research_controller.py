"""PostgreSQL-backed acceptance evidence for issue #310 research controller."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_evidence_service,
    build_production_resumable_orchestrator,
    build_run_service,
    build_semantic_service,
    build_uow_factory,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.coverage_seed_service import (
    CompleteCoverageService,
)
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.parsing import get_registry
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.research_controller import (
    ControllerPolicy,
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_COMPLETED,
    DISPOSITION_PARTIAL,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)
OBJECTIVE = "issue310 retained postgres controller authority"


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


@pytest.fixture
def controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResearchWorkflowController, CorpusService, list[str]]:
    monkeypatch.setenv("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        embedding_model=f"issue310-{uuid4().hex[:8]}",
        embedding_revision="test",
        embedding_dimension=4,
    )
    run_service = build_run_service(config)
    corpus = CorpusService(
        config,
        build_uow_factory(config),
        ContentAddressedBlobStore(config.blob_root),
        parser_registry=get_registry(),
    )
    coverage = CompleteCoverageService(run_service.uow_factory)
    provider_calls: list[str] = []

    def forbidden_provider_search(
        self: Any,
        query_text: str,
        **_kwargs: Any,
    ) -> Any:
        provider_calls.append(query_text)
        raise AssertionError("retained-first controller invoked Firecrawl provider")

    monkeypatch.setattr(
        BoundedFirecrawlSearchAdapter,
        "search",
        forbidden_provider_search,
    )

    workflow = ResearchWorkflowController(
        config=config,
        run_service=run_service,
        corpus_service=corpus,
        coverage_service=coverage,
        evidence_service=build_evidence_service(config),
        semantic_service=build_semantic_service(config),
        orchestrator_factory=lambda orchestrator_config: (
            build_production_resumable_orchestrator(
                config,
                orchestrator_config=orchestrator_config,
            )
        ),
        controller_config=None,
    )
    return workflow, corpus, provider_calls


def _seed_retained(corpus: CorpusService) -> None:
    corpus.ingest(
        IngestRequest(
            requested_url="https://issue310.example/retained",
            content=(
                b"Issue310 retained postgres controller authority is established "
                b"by this durable retained corpus evidence."
            ),
            title="Issue310 retained controller authority",
        )
    )


def test_retained_sufficient_completes_with_zero_provider_calls(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)

    result = workflow.run(
        OBJECTIVE,
        execution_mode="deterministic_debug",
    )

    assert result.disposition == DISPOSITION_COMPLETED
    assert result.result_ready is True
    assert result.objective_satisfied is True
    assert result.handoff_ready is True
    assert provider_calls == []


def test_retained_only_insufficient_is_partial_with_zero_provider_calls(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, _corpus, provider_calls = controller
    provider_calls.clear()

    result = workflow.run(
        f"absent retained evidence {uuid4().hex}",
        retained_only=True,
        execution_mode="deterministic_debug",
    )

    assert result.disposition == DISPOSITION_PARTIAL
    assert result.result_ready is True
    assert result.objective_satisfied is False
    assert provider_calls == []


@pytest.mark.parametrize(
    "restart_state",
    ["created", "planning", "corpus_review", "retrieving"],
)
def test_restart_from_early_automatic_transitions_uses_persisted_authority(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
    restart_state: str,
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(
        retained_only=False,
        evaluated_at=workflow.clock(),
    )
    workflow._record_policy(status, policy)
    bundle = workflow._initialize_planning(status, policy)

    if restart_state in {"planning", "corpus_review", "retrieving"}:
        status = workflow._transition(
            status,
            "planning",
            key=f"test:planning:{status.id}",
            reason="simulate persisted automatic transition",
        )
    if restart_state in {"corpus_review", "retrieving"}:
        status = workflow._transition(
            status,
            "corpus_review",
            key=f"test:corpus-review:{status.id}",
            reason="simulate persisted automatic transition",
        )
    if restart_state == "retrieving":
        status = workflow._enter_retained_review(status, bundle)

    result = workflow.continue_run(status.external_id or "")
    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []


def test_restart_from_retained_coverage_decision_is_deterministic(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(False, workflow.clock())
    workflow._record_policy(status, policy)
    bundle = workflow._initialize_planning(status, policy)
    status = workflow._transition(
        status,
        "planning",
        key=f"test:planning:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._transition(
        status,
        "corpus_review",
        key=f"test:corpus-review:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._enter_retained_review(status, bundle)
    evaluation = workflow.retained_review.evaluate(
        status,
        bundle,
        evaluated_at=policy.evaluated_at,
    )
    assert evaluation.outcome == "sufficient"
    status = workflow._transition(
        status,
        "coverage_review",
        key=f"test:coverage-review:{status.id}",
        reason="simulate crash after retained evaluation",
    )

    result = workflow.continue_run(status.external_id or "")
    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []


def test_no_controller_run_exposes_internal_identity_in_public_result(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, _corpus, _provider_calls = controller
    latest = workflow.run(
        f"absent retained evidence {uuid4().hex}",
        retained_only=True,
        execution_mode="deterministic_debug",
    )
    payload = latest.to_dict()
    assert payload["run_id"].startswith("fr_")
    for forbidden in (
        "research_spec_id",
        "search_plan_id",
        "invocation_id",
        "membership_fingerprint",
        "candidate_budget_check_id",
    ):
        assert forbidden not in payload
