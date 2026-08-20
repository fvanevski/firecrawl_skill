from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.acquisition.authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
)
from firecrawl_skill.research_store.acquisition.direct_scrape_application import (
    DirectScrapePersistenceError,
)
from firecrawl_skill.research_store.acquisition.models import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
)
from firecrawl_skill.research_store.acquisition.service import (
    AcquisitionResult,
    AcquisitionService,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult, utcnow
from firecrawl_skill.research_store.fsearch_service import (
    FSearchError,
    FSearchRequest,
    FSearchResult,
    FSearchService,
    MetadataOnlyFirecrawlSearchAdapter,
    build_fsearch_service,
    main,
)
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
RUN_EXTERNAL_ID = "fr_" + "a" * 32


@dataclass
class _ScrapeItem:
    candidate_id: UUID
    status: str = "succeeded"
    error: str | None = None
    extraction_attempt_id: UUID | None = None
    source_id: UUID | None = None
    snapshot_id: UUID | None = None
    document_id: UUID | None = None
    derivation_id: UUID | None = None
    chunk_ids: tuple[UUID, ...] = ()
    failure_class: str | None = None


class _FakeInvocationService:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict]] = []

    def begin(self, *args, **kwargs):
        return SimpleNamespace(id=uuid4())

    def complete(self, _run_id, _invocation_id, status, **kwargs):
        self.completed.append((status, kwargs))
        return SimpleNamespace(id=_invocation_id)


class _FakeAcquisitionService:
    def __init__(self, result: AcquisitionResult, events: list[str]) -> None:
        self.result = result
        self.events = events
        self.calls: list[dict] = []

    def execute_search(self, *_args, **kwargs) -> AcquisitionResult:
        self.events.append("search")
        self.calls.append(kwargs)
        return self.result


class _FakeDirectScrapeService:
    def __init__(self, result, events: list[str]) -> None:
        self.result = result
        self.events = events
        self.requests = ()
        self.calls: list[dict] = []

    def execute(self, _run_id, requests, **kwargs):
        self.events.append("extract")
        self.requests = requests
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _acquisition_result(
    *,
    status: str = "succeeded",
    candidates: list[dict] | None = None,
    committed: bool = True,
) -> AcquisitionResult:
    values = candidates or []
    return AcquisitionResult(
        search_response_id=uuid4(),
        run_id=uuid4(),
        query_text="test query",
        backend="firecrawl",
        status=status,
        candidate_count=len(values),
        candidates=values,
        postgres_committed=committed,
        search_response={"error_message": "provider diagnostic"},
    )


def _service(
    acquisition_result: AcquisitionResult,
    *,
    direct_result=None,
    preflight=None,
    classifier=None,
):
    events: list[str] = []
    run_id = uuid4()
    invocations = _FakeInvocationService()
    acquisition = _FakeAcquisitionService(acquisition_result, events)
    direct = _FakeDirectScrapeService(direct_result, events)
    run_service = SimpleNamespace(status=lambda **_kwargs: SimpleNamespace(id=run_id))

    def checked_preflight(**kwargs):
        events.append("preflight")
        if preflight is not None:
            return preflight(**kwargs)
        return SimpleNamespace(run_id=run_id)

    def acquisition_factory():
        events.append("acquisition_factory")
        return acquisition

    def direct_factory():
        events.append("direct_factory")
        return direct

    service = FSearchService(
        cast(StoreConfig, SimpleNamespace()),
        run_service,
        invocations,
        acquisition_factory=cast(Callable[[], AcquisitionService], acquisition_factory),
        direct_scrape_factory=direct_factory,
        preflight=cast(
            Callable[..., AuthoritativeAcquisitionContext], checked_preflight
        ),
        classify_target=classifier or (lambda *_args: ("other", False)),
        profiles={"news_article": {"target_schema": {"type": "object"}}},
    )
    return service, events, invocations, acquisition, direct


def test_preflight_precedes_transport_and_selected_scrape_uses_candidate_ids(
    tmp_path, monkeypatch
):
    candidate_ids = (uuid4(), uuid4())
    candidates = [
        {
            "id": candidate_ids[1],
            "rank": 2,
            "original_url": "https://example.org/two",
        },
        {
            "id": candidate_ids[0],
            "rank": 1,
            "original_url": "https://example.org/one",
            "title": "News",
            "snippet": "Reported by staff",
        },
    ]
    item = _ScrapeItem(
        candidate_id=candidate_ids[0],
        extraction_attempt_id=uuid4(),
        source_id=uuid4(),
        snapshot_id=uuid4(),
        document_id=uuid4(),
        derivation_id=uuid4(),
        chunk_ids=(uuid4(),),
    )
    direct_result = DirectScrapeBatchResult(
        run_id=uuid4(),
        invocation_id=uuid4(),
        idempotency_key="direct",
        status="complete",
        items=cast(tuple[DirectScrapeItemResult, ...], (item,)),
    )
    service, events, _invocations, acquisition, direct = _service(
        _acquisition_result(candidates=candidates),
        direct_result=direct_result,
        classifier=lambda *_args: ("news_article", True),
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    result = service.execute(
        FSearchRequest(
            "test query",
            RUN_EXTERNAL_ID,
            scrape_limit=1,
            profile="news_article",
        )
    )

    assert events == [
        "preflight",
        "acquisition_factory",
        "search",
        "direct_factory",
        "extract",
    ]
    assert "export_scratch" not in acquisition.calls[0]
    assert "scratch_dir" not in acquisition.calls[0]
    assert direct.requests[0].candidate_id == candidate_ids[0]
    assert direct.requests[0].url is None
    assert direct.requests[0].format == "json"
    assert direct.requests[0].schema == {"type": "object"}
    assert result.candidate_ids == candidate_ids
    assert result.search_response_id is not None
    assert result.extraction_invocation_id == direct_result.invocation_id
    output = result.to_dict()
    assert output["corpus_ids"]["document_ids"] == [str(item.document_id)]
    assert output["corpus_ids"]["chunk_ids"] == [str(item.chunk_ids[0])]
    assert list(tmp_path.iterdir()) == []


def test_failed_preflight_prevents_search_and_scrape_construction():
    def fail(**_kwargs):
        raise AcquisitionPreflightError("database unavailable")

    service, events, _invocations, _acquisition, _direct = _service(
        _acquisition_result(),
        preflight=fail,
    )

    with pytest.raises(FSearchError, match="database unavailable") as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID))

    assert caught.value.stage == "preflight"
    assert events == ["preflight"]


@pytest.mark.parametrize(
    ("status", "expected_stage"),
    [
        ("provider_error", "search_transport"),
        ("parse_error", "candidate_parsing"),
    ],
)
def test_search_failures_have_distinct_stages(status, expected_stage):
    service, _events, _invocations, _acquisition, _direct = _service(
        _acquisition_result(status=status)
    )

    with pytest.raises(FSearchError) as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID))

    assert caught.value.stage == expected_stage
    assert caught.value.result is not None
    assert caught.value.result.search_response_id is not None


def test_uncommitted_search_is_ingestion_failure():
    service, _events, _invocations, _acquisition, _direct = _service(
        _acquisition_result(committed=False)
    )

    with pytest.raises(FSearchError) as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID))

    assert caught.value.stage == "ingestion"


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        ("provider extraction failed", "extraction"),
        ("parser failure: invalid structured JSON", "ingestion"),
    ],
)
def test_item_failures_distinguish_extraction_and_ingestion(error, expected_stage):
    candidate_id = uuid4()
    direct_result = DirectScrapeBatchResult(
        run_id=uuid4(),
        invocation_id=uuid4(),
        idempotency_key="direct",
        status="failed",
        items=cast(
            tuple[DirectScrapeItemResult, ...],
            (
                _ScrapeItem(
                    candidate_id=candidate_id,
                    status="failed",
                    error=error,
                    failure_class=(
                        "parser" if expected_stage == "ingestion" else "network"
                    ),
                ),
            ),
        ),
    )
    service, _events, _invocations, _acquisition, _direct = _service(
        _acquisition_result(
            candidates=[
                {
                    "id": candidate_id,
                    "rank": 1,
                    "original_url": "https://example.org",
                }
            ]
        ),
        direct_result=direct_result,
    )

    with pytest.raises(FSearchError) as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID, scrape_limit=1))

    assert caught.value.stage == expected_stage
    assert caught.value.result is not None
    assert caught.value.result.extraction_outcomes[0].failure_stage == expected_stage


def test_index_persistence_failure_has_indexing_stage():
    candidate_id = uuid4()
    service, _events, _invocations, _acquisition, _direct = _service(
        _acquisition_result(
            candidates=[
                {
                    "id": candidate_id,
                    "rank": 1,
                    "original_url": "https://example.org",
                }
            ]
        ),
        direct_result=DirectScrapePersistenceError(
            "index job insert failed", stage="indexing"
        ),
    )

    with pytest.raises(FSearchError) as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID, scrape_limit=1))

    assert caught.value.stage == "indexing"


def test_idempotency_keys_are_stable_and_scope_selected_extraction():
    candidate_id = uuid4()
    direct_result = DirectScrapeBatchResult(
        run_id=uuid4(),
        invocation_id=uuid4(),
        idempotency_key="direct",
        status="complete",
        items=cast(
            tuple[DirectScrapeItemResult, ...],
            (_ScrapeItem(candidate_id=candidate_id),),
        ),
    )
    service, _events, _invocations, acquisition, direct = _service(
        _acquisition_result(
            candidates=[
                {
                    "id": candidate_id,
                    "rank": 1,
                    "original_url": "https://example.org",
                }
            ]
        ),
        direct_result=direct_result,
    )

    service.execute(
        FSearchRequest(
            "test query",
            RUN_EXTERNAL_ID,
            scrape_limit=1,
            idempotency_key="search-key",
        )
    )

    assert acquisition.calls[0]["idempotency_key"] == "search-key"
    assert direct.calls[0]["idempotency_key"].startswith("fsearch-extraction:")
    assert direct.calls[0]["parent_invocation_id"] is not None


def test_metadata_search_adapter_uses_stdout_without_implicit_scrape():
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"success":true,"data":[]}',
            stderr=b"",
        )

    result = MetadataOnlyFirecrawlSearchAdapter(
        runner=cast(Callable[..., CompletedProcess], runner)
    ).search("test query", limit=7, sources="web,news", tbs="qdr:w")

    assert result.http_status == 200
    assert calls[0][:3] == ["firecrawl", "search", "test query"]
    assert "--scrape" not in calls[0]
    assert "--scrape-formats" not in calls[0]
    assert "-o" not in calls[0]
    assert result.transport_metadata["implicit_scrape"] is False


def test_cli_json_output_is_bounded_authoritative_contract(capsys):
    result = FSearchResult(
        status="complete",
        run_id=uuid4(),
        research_run_id=RUN_EXTERNAL_ID,
        invocation_id=uuid4(),
        external_invocation_id="fc_" + "b" * 32,
        search_response_id=uuid4(),
        candidate_ids=(uuid4(),),
    )
    fake = SimpleNamespace(execute=lambda _request: result)

    code = main(
        ["test query", "--research-run-id", RUN_EXTERNAL_ID, "--json"],
        service_factory=cast(Callable[[], FSearchService], lambda: fake),
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "authoritative-fsearch-v1"
    assert output["run_id"] == str(result.run_id)
    assert output["invocation_id"] == str(result.invocation_id)
    assert output["search_response_id"] == str(result.search_response_id)
    assert output["candidate_ids"] == [str(result.candidate_ids[0])]


@pytest.mark.parametrize(
    "removed_args",
    [
        ["--dir", "/tmp/results"],
        ["--reuse-search"],
        ["--scrape-ranks", "1,2"],
    ],
)
def test_removed_file_and_rank_flags_fail_before_service_construction(
    removed_args,
):
    constructed = False

    def factory():
        nonlocal constructed
        constructed = True
        raise AssertionError("service must not be constructed")

    code = main(
        [
            "test query",
            "--research-run-id",
            RUN_EXTERNAL_ID,
            *removed_args,
        ],
        service_factory=factory,
    )

    assert code == 2
    assert constructed is False


@pytest.fixture(scope="session")
def prepared_database():
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 11


class _SuccessfulSearchAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, _query_text: str, **_kwargs) -> SearchAdapterResult:
        self.calls += 1
        return SearchAdapterResult(
            raw_payload=json.dumps(
                {
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "url": "https://example.org/one",
                                "title": "One",
                            },
                            {
                                "url": "https://example.org/two",
                                "title": "Two",
                            },
                        ]
                    },
                }
            ).encode(),
            http_status=200,
            provider_request_id="search-1",
            transport_error=None,
            transport_metadata={"test": True, "implicit_scrape": False},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_repeated_idempotency_key_reuses_response_and_candidates(
    tmp_path, prepared_database
):
    from firecrawl_skill.research_store.composition import (
        build_run_service,
        build_workflow_operation_service,
    )

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    run_service = build_run_service(config)
    external_run_id = f"fr_{uuid4().hex}"
    run = run_service.create(
        objective="authoritative fsearch idempotency",
        external_id=external_run_id,
    )
    build_workflow_operation_service(config).prepare_run(external_run_id)
    adapter = _SuccessfulSearchAdapter()
    service = build_fsearch_service(
        config,
        search_adapter_factory=lambda: adapter,
    )

    first = service.execute(
        FSearchRequest(
            "idempotent query",
            external_run_id,
            scrape_limit=0,
            idempotency_key="authoritative-fsearch-idempotency",
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    second = service.execute(
        FSearchRequest(
            "idempotent query",
            external_run_id,
            scrape_limit=0,
            idempotency_key="authoritative-fsearch-idempotency",
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )

    assert first.search_response_id == second.search_response_id
    assert first.candidate_ids == second.candidate_ids
    assert second.search_replayed is True
    assert adapter.calls == 1
    assert len(run_service.list_search_responses(run.id)) == 1
    assert len(run_service.list_candidates(run.id)) == 2
    assert not any(tmp_path.glob("**/_meta.json"))
    assert not any(tmp_path.glob("**/_search.json"))
