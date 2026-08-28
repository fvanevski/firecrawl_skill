"""Production-seam regression tests for issue #216.

The tests deliberately distinguish search discovery from candidate extraction,
exercise real bounded child-process cancellation, and verify that only suitable
content reaches corpus ingestion. PostgreSQL-backed coverage verifies that the
durable extraction-attempt taxonomy and audit text match the bounded outcome.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill import research_store
from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.bounded_orchestrator import BoundedExtractionStage
from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_extraction_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult
from firecrawl_skill.research_store.postgres import migrate
from firecrawl_skill.research_store.provider_preflight import (
    BoundedSubprocessRunner,
    CandidatePreflightChecker,
    CandidatePreflightResult,
    ExtractionDeadlinePolicy,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


def _wrapped_result(
    markdown: str,
    *,
    classification: str = "suitable",
    reason_code: str | None = None,
    failure_stage: str = "content_suitability",
    status: int = 200,
    content_type: str = "text/html",
    elapsed: float = 0.025,
    cancelled: bool = False,
    terminal: bool | None = None,
) -> SearchAdapterResult:
    if terminal is None:
        terminal = classification != "suitable"
    outcome = CandidatePreflightResult(
        classification=classification,
        reason_code=reason_code or classification,
        reason=f"test outcome {classification}",
        failure_stage=failure_stage,
        http_status=status,
        content_type=content_type,
        elapsed_seconds=elapsed,
        first_byte_seconds=min(0.005, elapsed),
        provider_operation_seconds=elapsed,
        cancelled=cancelled,
        retryable=False,
        terminal=terminal,
    )
    payload = {
        "success": True,
        "data": {
            "web": [
                {
                    "url": "https://example.test/item",
                    "markdown": markdown,
                    "metadata": {
                        "url": "https://example.test/item",
                        "statusCode": status,
                        "contentType": content_type,
                    },
                }
            ]
        },
    }
    return SearchAdapterResult(
        raw_payload=json.dumps(payload).encode(),
        http_status=status,
        transport_metadata={
            "elapsed_seconds": elapsed,
            "first_byte_seconds": min(0.005, elapsed),
            "provider_operation_seconds": elapsed,
            "content_type": content_type,
            "preflight": outcome.to_metadata(),
        },
    )


class TestCanonicalRouting:
    def test_canonical_adapter_and_composition_are_bounded(self):
        from firecrawl_skill.research_store.composition import (
            build_production_orchestrator,
        )

        assert (
            BoundedFirecrawlSearchAdapter.__module__
            == "firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl"
        )
        assert not hasattr(research_store, "FirecrawlSearchAdapter")
        # Composition root explicitly injects bounded stages
        from firecrawl_skill.research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )
        from firecrawl_skill.research_store.orchestrator import ResearchOrchestrator

        assert issubclass(CheckpointResearchOrchestrator, ResearchOrchestrator)
        # Verify the composition root passes bounded classes
        import inspect

        source = inspect.getsource(build_production_orchestrator)
        assert "BoundedAcquisitionStage" in source
        assert "BoundedExtractionStage" in source


class TestSuitabilityPolicy:
    def test_empty_markdown_is_terminal_empty_content(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(raw_payload=b'{"markdown":""}', http_status=200)
        outcome = checker.check(result)
        assert outcome.classification == "empty_content"
        assert outcome.reason_code == "empty_markdown"
        assert outcome.terminal
        assert outcome.cancelled

    def test_whitespace_markdown_is_terminal_empty_content(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(
            raw_payload=json.dumps({"markdown": "  \n\t  "}).encode(), http_status=200
        )
        assert checker.check(result).classification == "empty_content"

    def test_anti_bot_challenge_is_rejected(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(
            raw_payload=json.dumps(
                {"markdown": "Cloudflare verification challenge: verify you are human"}
            ).encode(),
            http_status=200,
            transport_metadata={"content_type": "text/html"},
        )
        outcome = checker.check(result)
        assert outcome.classification == "anti_bot"
        assert outcome.failure_stage == "content_suitability"

    def test_legitimate_bot_detection_article_is_not_false_positive(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(
            raw_payload=json.dumps(
                {
                    "markdown": (
                        "# Security research\n\n"
                        "This article compares bot detection mechanisms and "
                        "Cloudflare products in ordinary prose."
                    )
                }
            ).encode(),
            http_status=200,
            transport_metadata={"content_type": "text/markdown"},
        )
        outcome = checker.check(result)
        assert outcome.classification == "suitable"
        assert not outcome.terminal

    def test_unsupported_content_type_is_distinct(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(
            raw_payload=json.dumps({"markdown": "binary representation"}).encode(),
            http_status=200,
            transport_metadata={"content_type": "image/png"},
        )
        outcome = checker.check(result)
        assert outcome.classification == "unsupported_content_type"
        assert outcome.failure_stage == "content_type"

    def test_transient_transport_is_retryable_and_redacted(self):
        checker = CandidatePreflightChecker()
        result = SearchAdapterResult(
            raw_payload=b"{}",
            transport_error="token=super-secret EAI_AGAIN",
        )
        outcome = checker.check(result)
        assert outcome.classification == "transient"
        assert outcome.retryable
        assert not outcome.terminal
        assert "super-secret" not in outcome.reason
        assert "[REDACTED]" in outcome.reason

    def test_timeout_is_not_misclassified_as_empty_content(self):
        checker = CandidatePreflightChecker(max_elapsed_seconds=2.0)
        result = SearchAdapterResult(
            raw_payload=b"{}",
            transport_metadata={
                "elapsed_seconds": 2.1,
                "provider_operation_seconds": 2.1,
                "timeout_reason": "provider_operation_timeout",
            },
        )
        outcome = checker.check(result)
        assert outcome.classification == "timeout"
        assert outcome.reason_code == "provider_operation_timeout"
        assert outcome.failure_stage == "provider_operation"


class TestBoundedProviderExecution:
    def test_search_is_discovery_only(self):
        calls: list[list[str]] = []
        payload = json.dumps(
            {"success": True, "data": [{"url": "https://example.test/a"}]}
        ).encode()

        def runner(cmd, timeout=None):
            calls.append(list(cmd))
            return 0, payload, ""

        adapter = BoundedFirecrawlSearchAdapter(
            runner=runner,
            deadline_policy=ExtractionDeadlinePolicy(
                first_byte_timeout_seconds=1,
                provider_operation_timeout_seconds=2,
                overall_candidate_timeout_seconds=3,
            ),
        )
        result = adapter.search("bounded extraction")
        assert result.raw_payload == payload
        assert calls[0][1] == "search"
        assert "--scrape" not in calls[0]
        assert "--scrape-formats" not in calls[0]

    def test_empty_content_has_separate_zero_retry_default(self):
        calls = 0

        def runner(cmd, timeout=None):
            nonlocal calls
            calls += 1
            return (
                0,
                json.dumps(
                    {
                        "markdown": "",
                        "metadata": {
                            "statusCode": 200,
                            "contentType": "text/html",
                        },
                    }
                ).encode(),
                "",
            )

        adapter = BoundedFirecrawlSearchAdapter(
            runner=runner,
            deadline_policy=ExtractionDeadlinePolicy(
                first_byte_timeout_seconds=1,
                provider_operation_timeout_seconds=2,
                overall_candidate_timeout_seconds=3,
                transient_retries=2,
                empty_content_retries=0,
            ),
        )
        result = adapter.scrape_url("https://example.test/empty")
        assert calls == 1
        assert (
            result.transport_metadata["preflight"]["classification"] == "empty_content"
        )

    def test_transient_failure_retries_then_succeeds(self):
        responses = [
            (1, b"", "EAI_AGAIN"),
            (
                0,
                json.dumps(
                    {
                        "markdown": "# useful",
                        "metadata": {
                            "statusCode": 200,
                            "contentType": "text/html",
                        },
                    }
                ).encode(),
                "",
            ),
        ]

        def runner(cmd, timeout=None):
            return responses.pop(0)

        adapter = BoundedFirecrawlSearchAdapter(
            runner=runner,
            deadline_policy=ExtractionDeadlinePolicy(
                first_byte_timeout_seconds=1,
                provider_operation_timeout_seconds=2,
                overall_candidate_timeout_seconds=3,
                transient_retries=2,
            ),
        )
        result = adapter.scrape_url("https://example.test/transient")
        assert result.transport_metadata["attempts"] == 2
        assert result.transport_metadata["preflight"]["classification"] == "suitable"

    def test_first_byte_timeout_reaps_provider_process(self, tmp_path):
        pid_path = tmp_path / "provider.pid"
        command = [
            "/bin/sh",
            "-c",
            f"echo $$ > {pid_path}; exec sleep 5",
        ]
        result = BoundedSubprocessRunner().run(
            command,
            first_byte_timeout_seconds=0.75,
            operation_timeout_seconds=2.0,
        )
        assert result.timeout_reason == "first_byte_timeout"
        assert result.cancelled
        assert pid_path.exists()
        pid = int(pid_path.read_text().strip())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_provider_operation_timeout_after_first_byte(self):
        result = BoundedSubprocessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys,time;print('x',flush=True);time.sleep(5)",
            ],
            first_byte_timeout_seconds=2.0,
            operation_timeout_seconds=0.75,
        )
        assert result.first_byte_seconds is not None
        assert result.timeout_reason == "provider_operation_timeout"
        assert result.cancelled


class _FakeExtractionService:
    def __init__(self):
        self.created: list[tuple[UUID, dict]] = []
        self.completed: list[dict] = []
        self.selected: list[dict] = []

    def create_attempt(self, **kwargs):
        attempt_id = uuid4()
        self.created.append((attempt_id, kwargs))
        return attempt_id

    def complete_attempt(self, **kwargs):
        self.completed.append(kwargs)
        return SimpleNamespace(**kwargs)

    def store_raw_blob(self, content):
        return SimpleNamespace(
            sha256="a" * 64, uri="blob:raw", byte_length=len(content)
        )

    def store_normalized_blob(self, content):
        return SimpleNamespace(
            sha256="b" * 64, uri="blob:normalized", byte_length=len(content)
        )

    def select_final_attempt(self, **kwargs):
        self.selected.append(kwargs)


class _FakeCorpusService:
    def __init__(self):
        self.calls: list[dict] = []
        self.last_assets: list[dict] = []

    def ingest_batch(self, **kwargs):
        self.calls.append(kwargs)
        assets = []
        for fallback, item in enumerate(kwargs["requests"]):
            ordinal = (
                item.get("metadata", {})
                .get("firecrawl", {})
                .get("result_index", fallback)
            )
            request = item.get("request")
            if request is not None:
                assets.append(
                    {
                        "ordinal": ordinal,
                        "status": "complete",
                        "requested_url": request.requested_url,
                        "snapshot_id": str(uuid4()),
                        "chunk_ids": [str(uuid4())],
                        "extraction_attempt_id": str(item["extraction_attempt_id"]),
                    }
                )
            else:
                assets.append(
                    {
                        "ordinal": ordinal,
                        "status": "failed",
                        "requested_url": item["requested_url"],
                        "snapshot_id": None,
                        "chunk_ids": [],
                        "error": item.get("error"),
                        "extraction_attempt_id": str(item["extraction_attempt_id"]),
                    }
                )
        self.last_assets = assets
        return {"batch_id": str(uuid4()), "assets": assets, "failure_count": 0}

    def finalize_ingestion_batch(self, batch_id, status, error=None):
        return {
            "batch_id": batch_id,
            "status": status,
            "assets": list(self.last_assets),
            "failure_count": sum(
                1 for item in self.last_assets if item["status"] == "failed"
            ),
        }

    def bounded_ingest_batch(self, **kwargs):
        # Mirror ingest_batch semantics for the bounded path.
        return self.ingest_batch(**kwargs)


class _FakeScrapeAdapter:
    def __init__(self, by_url):
        self.by_url = by_url
        self.calls: list[str] = []

    def scrape_url(self, url):
        self.calls.append(url)
        return self.by_url[url]


class TestProductionExtractionSeam:
    def _stage(self):
        run_service = MagicMock()
        run_service.status.return_value = SimpleNamespace(external_id="fr_test")
        extraction = _FakeExtractionService()
        corpus = _FakeCorpusService()
        stage = BoundedExtractionStage(
            run_service=run_service,
            coverage_service=MagicMock(),
            config=SimpleNamespace(parser_version="parser-v1"),
            corpus_service=corpus,
            extraction_service=extraction,
        )
        return stage, run_service, extraction, corpus

    def test_suitable_candidate_ingests_while_empty_candidate_is_cancelled(self):
        stage, _run_service, extraction, corpus = self._stage()
        good = "https://example.test/good"
        empty = "https://example.test/empty"
        context: dict[str, Any] = {
            "raw_ingest_requests": [
                {
                    "requested_url": good,
                    "metadata": {
                        "candidate_id": str(uuid4()),
                        "firecrawl": {"result_index": 0},
                    },
                },
                {
                    "requested_url": empty,
                    "metadata": {
                        "candidate_id": str(uuid4()),
                        "firecrawl": {"result_index": 1},
                    },
                },
            ]
        }
        context["_candidate_scrape_adapter"] = _FakeScrapeAdapter(
            {
                good: _wrapped_result("# useful evidence"),
                empty: _wrapped_result(
                    "",
                    classification="empty_content",
                    reason_code="empty_markdown",
                    cancelled=True,
                ),
            }
        )
        result = stage.execute(uuid4(), 4, 1, "extracting", context)
        assert result.error is None
        assert len(corpus.calls) == 1
        assert len(corpus.calls[0]["requests"]) == 2
        assert corpus.calls[0]["requests"][0]["requested_url"] == good
        assert corpus.calls[0]["requests"][1]["requested_url"] == empty
        assert "request" not in corpus.calls[0]["requests"][1]
        assert corpus.calls[0]["requests"][1].get("extraction_attempt_id") is not None
        assert any(item["exit_status"] == "succeeded" for item in extraction.completed)
        rejected = [
            item
            for item in extraction.completed
            if item.get("failure_class") == "empty_content"
        ]
        assert len(rejected) == 1
        assert rejected[0]["exit_status"] == "cancelled"
        assert "reason_code=empty_markdown" in rejected[0]["error_message"]
        assert "stage=content_suitability" in rejected[0]["error_message"]

    def test_unsupported_content_type_uses_existing_durable_enum(self):
        stage, _run_service, extraction, corpus = self._stage()
        url = "https://example.test/image"
        context = {
            "raw_ingest_requests": [
                {
                    "requested_url": url,
                    "metadata": {
                        "candidate_id": str(uuid4()),
                        "firecrawl": {"result_index": 0},
                    },
                }
            ],
            "_candidate_scrape_adapter": _FakeScrapeAdapter(
                {
                    url: _wrapped_result(
                        "binary",
                        classification="unsupported_content_type",
                        reason_code="unsupported_content_type",
                        failure_stage="content_type",
                        content_type="image/png",
                        cancelled=True,
                    )
                }
            ),
        }
        result = stage.execute(uuid4(), 7, 1, "extracting", context)
        assert result.error is None
        assert len(corpus.calls) == 1
        assert len(corpus.calls[0]["requests"]) == 1
        assert "request" not in corpus.calls[0]["requests"][0]
        assert corpus.calls[0]["requests"][0].get("extraction_attempt_id") is not None
        assert extraction.completed[0]["failure_class"] == "unsupported_format"
        assert extraction.completed[0]["exit_status"] == "cancelled"


class _DiscoveryAdapter:
    def search(self, query_text: str, **kwargs):
        return SearchAdapterResult(
            raw_payload=json.dumps(
                {
                    "success": True,
                    "data": [
                        {
                            "url": "https://example.test/db-empty",
                            "title": "DB empty",
                        }
                    ],
                }
            ).encode(),
            http_status=200,
            transport_metadata={"attempt": 1},
        )


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL test DSN")
def test_postgres_audit_readback_preserves_class_stage_elapsed_and_redaction(tmp_path):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    external_id = f"fr_{uuid4()}"
    run_service = build_run_service(config)
    run_service.create(objective="issue 216 audit readback", external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    status = run_service.status(external_id=external_id)
    acquisition = build_acquisition_service(config, search_adapter=_DiscoveryAdapter())
    discovered = acquisition.execute_search(status.id, "db audit candidate")
    candidate_id = UUID(str(discovered.candidates[0]["candidate_id"]))

    status = run_service.status(run_id=status.id)
    run_service.transition(
        status.id,
        "extracting",
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"issue216:test:extracting:{status.id}",
        actor_type="test",
        actor_identifier="test_issue_216",
        triggering_event="run.extracting",
        reason="exercise bounded preflight audit persistence",
    )
    status = run_service.status(run_id=status.id)
    rejection = CandidatePreflightResult(
        classification="timeout",
        reason_code="provider_operation_timeout",
        reason="token=secret-value provider operation deadline exceeded",
        failure_stage="provider_operation",
        http_status=None,
        elapsed_seconds=1.25,
        first_byte_seconds=0.10,
        provider_operation_seconds=1.25,
        cancelled=True,
        terminal=True,
    )
    stage = BoundedExtractionStage(
        run_service=run_service,
        coverage_service=MagicMock(),
        config=config,
        corpus_service=_FakeCorpusService(),
        extraction_service=build_extraction_service(config),
    )
    context = {
        "raw_ingest_requests": [
            {
                "requested_url": "https://example.test/db-empty",
                "metadata": {
                    "candidate_id": str(candidate_id),
                    "firecrawl": {"result_index": 0},
                },
            }
        ],
        "_candidate_scrape_adapter": _FakeScrapeAdapter(
            {
                "https://example.test/db-empty": SearchAdapterResult(
                    raw_payload=b"{}",
                    transport_metadata={
                        "elapsed_seconds": 1.25,
                        "preflight": rejection.to_metadata(),
                    },
                )
            }
        ),
    }
    result = stage.execute(
        status.id, status.lifecycle_revision, 1, "extracting", context
    )
    assert result.error is None

    with run_service.uow_factory() as uow:
        attempts = uow.extraction_attempts.list_attempts_for_candidate(
            candidate_id, run_id=status.id
        )
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["exit_status"] == "cancelled"
    assert attempt["failure_class"] == "timeout"
    assert attempt["backend_status"] == (
        "preflight:provider_operation:provider_operation_timeout"
    )
    assert "elapsed_seconds=1.250000" in attempt["error_message"]
    assert "stage=provider_operation" in attempt["error_message"]
    assert "secret-value" not in attempt["error_message"]
    assert "[REDACTED]" in attempt["error_message"]
    assert attempt.get("raw_blob") is None
    assert attempt.get("normalized_blob") is None


def test_issue_216_documentation_and_ci_contract():
    root = SCRIPTS.parent
    doc = (root / "references" / "extraction-preflight-timeouts.md").read_text(
        encoding="utf-8"
    )
    authority = tomllib.loads(
        (root / "ci" / "test-profiles.toml").read_text(encoding="utf-8")
    )
    acquisition = authority["profiles"]["acquisition"]
    for setting in (
        "FIRECRAWL_EXTRACTION_FIRST_BYTE_TIMEOUT_SECONDS",
        "FIRECRAWL_EXTRACTION_PROVIDER_TIMEOUT_SECONDS",
        "FIRECRAWL_EXTRACTION_CANDIDATE_TIMEOUT_SECONDS",
        "FIRECRAWL_EXTRACTION_TRANSIENT_RETRIES",
        "FIRECRAWL_EXTRACTION_EMPTY_RETRIES",
    ):
        assert setting in doc
    assert "unsupported_content_type" in doc
    assert "unsupported_format" in doc
    assert "extraction_preflight" in acquisition["ownership_tokens"]
    assert set(acquisition["services"]) == {"postgres", "qdrant"}
