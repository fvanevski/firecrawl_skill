"""Exact runtime regressions for the independent review of issue #262."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

# Importing the public package installs the issue #217 runtime compatibility
# contract on BoundedExtractionStage.  The regression below deliberately tests
# that effective runtime method rather than only inspecting its source module.
import research_store
from research_store.acquisition.models import SearchAdapterResult
from research_store.bounded_orchestrator import BoundedExtractionStage
from research_store.ingestion_batch_semantics import _bounded_extraction_execute
from research_store.orchestration import composition


class _RecordingCandidateScrapeAdapter:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def scrape_url(
        self,
        url: str,
        *,
        transient_retries: int | None = None,
    ) -> SearchAdapterResult:
        self.urls.append(url)
        return SearchAdapterResult(
            raw_payload=b"{}",
            http_status=404,
            transport_metadata={
                "preflight": {
                    "classification": "http_error",
                    "reason_code": "review_terminal_http_error",
                    "reason": "review fixture terminal provider response",
                    "failure_stage": "response_status",
                    "http_status": 404,
                    "cancelled": True,
                    "retryable": False,
                    "terminal": True,
                }
            },
        )


def test_runtime_issue_217_execute_uses_production_candidate_scrape_port(
    monkeypatch,
) -> None:
    """Production composition must survive the #217 runtime method install.

    This closes the independent-review gap left by source-only structural tests:
    after the ordinary ``research_store`` import has replaced
    ``BoundedExtractionStage.execute`` with the issue #217 compatibility method,
    a production stage constructed without an explicit scrape adapter must still
    receive the bounded candidate transport from the composition root and use
    that injected port for a provider-needed candidate.
    """

    assert research_store.ResearchOrchestrator is not None
    assert BoundedExtractionStage.execute is _bounded_extraction_execute

    adapter = _RecordingCandidateScrapeAdapter()
    monkeypatch.setattr(
        composition,
        "BoundedFirecrawlSearchAdapter",
        lambda: adapter,
    )

    run_id = uuid4()
    candidate_id = uuid4()
    attempt_id = uuid4()
    batch_id = uuid4()
    requested_url = "https://example.com/review-runtime-port"

    run_service = mock.Mock()
    run_service.status.return_value = SimpleNamespace(external_id="fr_review_runtime")
    coverage_service = mock.Mock()
    extraction_service = mock.Mock()
    extraction_service.create_attempt.return_value = attempt_id
    corpus_service = mock.Mock()
    corpus_service.bounded_ingest_batch.return_value = {
        "batch_id": batch_id,
        "assets": [
            {
                "ordinal": 0,
                "status": "failed",
                "requested_url": requested_url,
            }
        ],
        "failure_count": 1,
    }
    corpus_service.finalize_ingestion_batch.return_value = {
        "batch_id": batch_id,
        "status": "failed",
        "assets": [
            {
                "ordinal": 0,
                "status": "failed",
                "requested_url": requested_url,
            }
        ],
        "outcome_summary": {"failed": 1, "cancelled": 0},
    }

    stage = composition.ProductionBoundedExtractionStage(
        run_service,
        coverage_service,
        SimpleNamespace(parser_version="review-parser"),
        corpus_service=corpus_service,
        extraction_service=extraction_service,
    )

    assert stage.scrape_adapter is adapter

    result = stage.execute(
        run_id,
        run_revision=3,
        coverage_revision=None,
        run_state="extracting",
        context={
            "raw_ingest_requests": [
                {
                    "requested_url": requested_url,
                    "metadata": {
                        "candidate_id": str(candidate_id),
                        "firecrawl": {"result_index": 0},
                    },
                }
            ],
            "candidate_coverage_items": {},
        },
    )

    assert adapter.urls == [requested_url]
    extraction_service.create_attempt.assert_called_once()
    extraction_service.complete_attempt.assert_called_once()
    batch_call = corpus_service.bounded_ingest_batch.call_args
    assert batch_call is not None
    batch_item = batch_call.kwargs["requests"][0]
    assert batch_item["extraction_attempt_id"] == attempt_id
    assert batch_item["requested_url"] == requested_url
    corpus_service.finalize_ingestion_batch.assert_called_once_with(batch_id, "failed")
    run_service.transition.assert_called_once()
    assert result.error is None
    assert result.details is not None
    assert result.details["preflight_terminal_count"] == 1
    assert result.details["batch_id"] == batch_id
