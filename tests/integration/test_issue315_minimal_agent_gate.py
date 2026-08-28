"""Independent service-backed epic-gate scenarios for issue #315."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

import firecrawl_skill.research_store.composition as composition_module
import firecrawl_skill.research_store.retrieval.projection.indexing as indexing_module
from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_evidence_service,
    build_invocation_service,
    build_run_service,
    build_semantic_service,
    build_service,
    build_uow_factory,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.coverage_seed_service import CompleteCoverageService
from firecrawl_skill.research_store.domain import (
    IngestRequest,
    SearchAdapterResult,
    utcnow,
)
from firecrawl_skill.research_store.index_admin import index_build
from firecrawl_skill.research_store.operator_action_service import (
    ACTION_BUDGET,
    ACTION_CURATION,
    ACTION_SCOPE,
)
from firecrawl_skill.research_store.parsing import get_registry
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_OPERATOR,
    ResearchResult,
    WorkflowDirective,
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


class _InlineMarkdownSearchAdapter:
    """Deterministic provider boundary that still exercises acquisition persistence."""

    def __init__(self, *, temporal: bool = False) -> None:
        self.temporal = temporal
        self.calls: list[str] = []

    def search(self, query_text: str, **_kwargs: Any) -> SearchAdapterResult:
        self.calls.append(query_text)
        urls = (
            "https://docs.python.org/3/reference/",
            "https://www.postgresql.org/docs/current/",
            "https://qdrant.tech/documentation/",
        )
        rows: list[dict[str, Any]] = []
        for index, source_url in enumerate(urls, start=1):
            markdown = (
                "# Historical authority\n\n"
                "This source intentionally has no publication/update timestamp and "
                "therefore cannot satisfy a current one-day freshness requirement."
                if self.temporal
                else (
                    "# Minimal-agent orchestration authority\n\n"
                    "The issue 315 gate source states that controller-owned research "
                    "performs bounded acquisition, deterministic candidate admission, "
                    "extraction, indexing, coverage evaluation, and host handoff "
                    "without outer-agent lifecycle choreography. "
                    f"Independent source {index}."
                )
            )
            rows.append(
                {
                    "url": source_url,
                    "title": f"Issue 315 authoritative source {index}",
                    "description": "deterministic gate provider fixture",
                    "markdown": markdown,
                    "metadata": {
                        "sourceURL": source_url,
                        "url": source_url,
                        "statusCode": 200,
                        "contentType": "text/markdown",
                    },
                }
            )
        payload = {"success": True, "data": rows}
        now = utcnow()
        return SearchAdapterResult(
            raw_payload=json.dumps(payload).encode("utf-8"),
            http_status=200,
            provider_request_id=f"issue315-{len(self.calls)}",
            transport_error=None,
            transport_metadata={"attempt": 1, "attempts": 1, "gate_fixture": True},
            requested_at=now,
            responded_at=now,
        )


class _EmbeddingResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.payload


def _install_deterministic_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, *_args: Any, **_kwargs: Any) -> _EmbeddingResponse:
        body = json.loads(bytes(request.data or b"{}").decode("utf-8"))
        inputs = body.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        payload = {
            "data": [
                {"index": index, "embedding": [1.0, 0.0, 0.0]}
                for index, _value in enumerate(inputs)
            ]
        }
        return _EmbeddingResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(indexing_module, "urlopen", fake_urlopen)


def _config(tmp_path: Path, label: str, *, vector: bool = False) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / f"{label}-blobs",
        embedding_url="http://issue315.invalid/v1" if vector else "",
        embedding_model=f"issue315-{label}-{uuid4().hex[:8]}",
        embedding_revision="gate",
        embedding_dimension=3,
    )


def _controller(
    config: StoreConfig,
    *,
    corpus_service: Any,
) -> ResearchWorkflowController:
    run_service = build_run_service(config)
    return ResearchWorkflowController(
        config=config,
        run_service=run_service,
        invocation_service=build_invocation_service(config),
        corpus_service=corpus_service,
        coverage_service=CompleteCoverageService(run_service.uow_factory),
        evidence_service=build_evidence_service(config),
        semantic_service=build_semantic_service(config),
        orchestrator_factory=lambda orchestrator_config: (
            composition_module.build_production_resumable_orchestrator(
                config,
                orchestrator_config=orchestrator_config,
            )
        ),
    )


def _retained_corpus(config: StoreConfig) -> CorpusService:
    return CorpusService(
        config,
        build_uow_factory(config),
        ContentAddressedBlobStore(config.blob_root),
        parser_registry=get_registry(),
    )


def _seed_retained(
    corpus: CorpusService,
    *,
    objective: str,
    count: int,
) -> None:
    for index in range(count):
        content = (
            f"{objective}\n\n"
            "Controller-owned orchestration and evidence handoff are authoritative. "
            f"Independent retained source {index + 1}."
        ).encode()
        corpus.ingest(
            IngestRequest(
                requested_url=f"https://retained.issue315.example/{index}",
                content=content,
                title=f"Retained issue315 authority {index + 1}",
            )
        )


def _forbid_provider_search(
    monkeypatch: pytest.MonkeyPatch,
    provider_calls: list[str],
) -> None:
    def forbidden_search(
        _self: BoundedFirecrawlSearchAdapter,
        query_text: str,
        **_kwargs: Any,
    ) -> SearchAdapterResult:
        provider_calls.append(query_text)
        raise AssertionError("retained-first gate unexpectedly invoked provider search")

    monkeypatch.setattr(BoundedFirecrawlSearchAdapter, "search", forbidden_search)


def _provider_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
    temporal: bool = False,
) -> tuple[ResearchWorkflowController, _InlineMarkdownSearchAdapter]:
    config = _config(tmp_path, label, vector=True)
    _install_deterministic_embedding(monkeypatch)
    index_setup = index_build(config)
    assert index_setup["index_definition"]["physical_collection"] == (
        config.physical_collection
    )
    assert index_setup["qdrant_schema"]["compatible"] is True
    adapter = _InlineMarkdownSearchAdapter(temporal=temporal)

    def deterministic_search(
        _self: BoundedFirecrawlSearchAdapter,
        query_text: str,
        **kwargs: Any,
    ) -> SearchAdapterResult:
        return adapter.search(query_text, **kwargs)

    monkeypatch.setattr(BoundedFirecrawlSearchAdapter, "search", deterministic_search)
    workflow = _controller(config, corpus_service=build_service(config))
    return workflow, adapter


def test_candidate_budget_soft_gate_uses_public_action_and_same_run_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objective = "issue315 retained candidate budget authorization"
    config = _config(tmp_path, "budget-soft")
    corpus = _retained_corpus(config)
    _seed_retained(corpus, objective=objective, count=1)
    provider_calls: list[str] = []
    _forbid_provider_search(monkeypatch, provider_calls)

    workflow = _controller(config, corpus_service=corpus)
    workflow.retained_completion.candidate_budget = replace(
        CandidateBudget(),
        max_per_asset_contribution_chunks=0,
    )

    directive = workflow.run(
        objective,
        execution_mode="deterministic_debug",
    )

    assert isinstance(directive, WorkflowDirective)
    assert directive.disposition == DISPOSITION_OPERATOR
    assert directive.action_kind == ACTION_BUDGET
    assert directive.action_id is not None and directive.action_id.startswith("oa_")
    original_run_id = directive.run_id

    public_action = workflow.action(directive.action_id)
    assert public_action["kind"] == ACTION_BUDGET
    assert public_action["status"] == "pending"
    public_text = json.dumps(public_action, sort_keys=True)
    for forbidden in (
        "check_id",
        "violated_limits",
        "lifecycle_revision",
        "scope_fingerprint",
    ):
        assert forbidden not in public_text

    result = workflow.approve(
        directive.action_id,
        reason="authorize the exact persisted soft candidate-budget boundary",
        authorized_by="issue315-gate",
    )

    assert isinstance(result, ResearchResult)
    assert result.run_id == original_run_id
    assert result.lifecycle_state == "completed"
    assert result.result_ready is True
    assert result.handoff_ready is True
    assert result.delivery_mode == "host_handoff"
    assert provider_calls == []
    resolved = workflow.action(directive.action_id)
    assert resolved["status"] == "resolved"


def test_autonomous_acquisition_reaches_terminal_host_handoff_without_outer_choreography(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, adapter = _provider_controller(
        tmp_path,
        monkeypatch,
        label="autonomous",
    )

    response = workflow.run(
        "issue315 minimal agent autonomous acquisition authority",
        execution_mode="deterministic_debug",
    )

    assert isinstance(response, ResearchResult)
    assert response.lifecycle_state == "completed"
    assert response.result_ready is True
    assert response.handoff_ready is True
    assert response.delivery_mode == "host_handoff"
    assert response.handoff is not None
    assert adapter.calls
    assert len(adapter.calls) <= 10

    status = workflow.run_service.status(external_id=response.run_id)
    with workflow.run_service.uow_factory() as uow:
        events = uow.runs.list_events(status.id, limit=500, offset=0)
    event_types = {str(item["event_type"]) for item in events}
    assert "acquisition.search_executed" in event_types
    assert "run.extracting" in event_types
    assert "run.indexing" in event_types
    assert "run.coverage_review" in event_types
    assert "run.completed" in event_types


def test_temporal_exhaustion_becomes_one_durable_scope_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, adapter = _provider_controller(
        tmp_path,
        monkeypatch,
        label="temporal",
        temporal=True,
    )

    response = workflow.run(
        "latest issue315 temporal authority in the past 1 day",
        execution_mode="deterministic_debug",
    )

    assert isinstance(response, WorkflowDirective)
    assert response.disposition == DISPOSITION_OPERATOR
    assert response.action_kind == ACTION_SCOPE
    assert response.action_id is not None and response.action_id.startswith("oa_")
    assert adapter.calls
    assert len(adapter.calls) <= 10

    public_action = workflow.action(response.action_id)
    assert public_action["kind"] == ACTION_SCOPE
    assert public_action["status"] == "pending"
    assert public_action["public_payload"]["scope_change_required"] is True

    status = workflow.run_service.status(external_id=response.run_id)
    with workflow.run_service.uow_factory() as uow:
        gaps = uow.runs.list_events(
            status.id,
            event_type="evidence.temporal_coverage_gap",
            limit=100,
            offset=0,
        )
        resolutions = uow.runs.list_events(
            status.id,
            event_type="evidence.temporal_coverage_resolved",
            limit=100,
            offset=0,
        )
    assert gaps
    assert resolutions == []
    assert workflow.status(response.run_id).action_id == response.action_id

    parent_before = workflow.run_service.status(external_id=response.run_id)
    child_response = workflow.fork(
        response.action_id,
        "issue315 revised historical authority without the exhausted temporal scope",
        reason="materially revise the objective instead of relaxing the parent in place",
        authorized_by="issue315-gate",
    )
    assert child_response.run_id != response.run_id
    assert child_response.run_id.startswith("fr_")
    parent_after = workflow.run_service.status(external_id=response.run_id)
    assert parent_after.id == parent_before.id
    assert parent_after.objective == parent_before.objective
    assert parent_after.lifecycle_revision == parent_before.lifecycle_revision
    resolved_action = workflow.action(response.action_id)
    assert resolved_action["status"] == "resolved"


def test_curated_mode_uses_one_selection_then_controller_completes_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objective = "issue315 curated retained authority"
    config = _config(tmp_path, "curated")
    corpus = _retained_corpus(config)
    _seed_retained(corpus, objective=objective, count=2)
    provider_calls: list[str] = []
    _forbid_provider_search(monkeypatch, provider_calls)
    workflow = _controller(config, corpus_service=corpus)

    directive = workflow.run(
        objective,
        curated=True,
        execution_mode="deterministic_debug",
    )
    assert isinstance(directive, WorkflowDirective)
    assert directive.disposition == DISPOSITION_OPERATOR
    assert directive.action_kind == ACTION_CURATION
    assert directive.action_id is not None and directive.action_id.startswith("oa_")

    action = workflow.action(directive.action_id)
    subjects = list(action["public_payload"]["subjects"])
    assert len(subjects) >= 2
    retained_subject = UUID(str(subjects[0]["subject_id"]))

    result = workflow.curate(
        directive.action_id,
        retain_subject_ids=[retained_subject],
        reject_rest=True,
        reason="retain one bounded authoritative subject for the independent epic gate",
        authorized_by="issue315-gate",
    )

    assert isinstance(result, ResearchResult)
    assert result.lifecycle_state == "completed"
    assert result.result_ready is True
    assert result.handoff_ready is True
    assert result.delivery_mode == "host_handoff"
    assert provider_calls == []
    resolved = workflow.action(directive.action_id)
    assert resolved["status"] == "resolved"
    assert resolved["resolution"]["payload"]["decision"] == "curated"
