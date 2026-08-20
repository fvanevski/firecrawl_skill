"""Neutral contracts and helpers for resumable research orchestration.

This module owns resume-specific constants, errors, and deterministic reconstruction
helpers shared by the compatibility facade and the canonical resume use case.  It
has no PostgreSQL access and no dependency on ``smart_orchestrator``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..domain import IngestRequest
from ..stages import ContextKeys

NETWORK_ENTRY_STATES = frozenset(
    {"created", "planning", "corpus_review", "coverage_review", "acquiring"}
)
PLANNING_STATES = frozenset({"created", "planning"})
TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})


class SmartResumeError(RuntimeError):
    """Persisted smart-run state cannot be resumed without guessing."""


def coverage_context(orchestrator: Any, run_id: UUID) -> dict[str, Any]:
    """Rebuild the authoritative coverage projection needed by resume control flow."""
    ledger = orchestrator.coverage_service.rebuild_projection(run_id)
    status = getattr(getattr(ledger, "overall_status", None), "value", None)
    items: list[dict[str, Any]] = []
    targets: dict[str, list[str]] = {}
    for item in getattr(ledger, "items", ()):
        item_id = str(item.coverage_item_id)
        items.append(
            {
                "coverage_item_id": item_id,
                "item_type": getattr(item.item_type, "value", str(item.item_type)),
                "subject_id": str(item.subject_id),
                "remaining_gap": str(item.remaining_gap or ""),
            }
        )
        for candidate_id in getattr(item, "candidate_ids", ()):
            targets.setdefault(str(candidate_id), []).append(item_id)
    context: dict[str, Any] = {
        ContextKeys.COVERAGE_LEDGER: ledger,
        "coverage_items": items,
        "candidate_coverage_items": targets,
        "coverage_revision": int(getattr(ledger, "revision", 0) or 0),
    }
    if status:
        context[ContextKeys.COVERAGE_STATUS] = status
        context[ContextKeys.OVERALL_STATUS] = status
    return context


def replay_extraction_inputs(
    orchestrator: Any,
    run_id: UUID,
    context: dict[str, Any],
    *,
    completed_candidates: set[str],
) -> list[dict[str, Any]]:
    """Recreate unprocessed ingest requests from authoritative response records.

    The caller supplies the completed-candidate projection through the resume-state
    port.  This keeps infrastructure reads outside the canonical application helper
    while preserving the historical replay behavior.
    """
    completed = {str(candidate_id) for candidate_id in completed_candidates}
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for response in orchestrator.run_service.list_search_responses(run_id):
        if response.get("backend") == "orchestrator":
            continue
        if response.get("status") == "failed":
            continue
        occurrences = orchestrator.run_service.record_response_candidates(
            run_id, UUID(str(response["id"]))
        )
        for occurrence in occurrences:
            candidate_id = str(occurrence.get("candidate_id") or "")
            if not candidate_id or candidate_id in completed or candidate_id in seen:
                continue
            seen.add(candidate_id)
            raw_item = occurrence.get("raw_item") or {}
            firecrawl = raw_item.get("metadata") or {}
            url = occurrence.get("canonical_url") or occurrence.get("original_url")
            metadata = {
                "candidate_id": candidate_id,
                "candidate_occurrence_id": str(occurrence.get("id")),
                "search_response_id": str(response["id"]),
                "resume_replay": True,
                "firecrawl": {
                    "result_index": int(occurrence.get("rank") or 0),
                    "scrape_id": firecrawl.get("scrapeId"),
                    "source_url": firecrawl.get("sourceURL") or url,
                    "status_code": firecrawl.get("statusCode"),
                },
            }
            markdown = raw_item.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                requests.append(
                    {
                        "request": IngestRequest(
                            requested_url=url,
                            final_url=firecrawl.get("url")
                            or firecrawl.get("sourceURL")
                            or url,
                            content=markdown.encode(),
                            normalized_content=markdown.encode(),
                            mime_type="text/markdown",
                            title=occurrence.get("title"),
                            http_status=firecrawl.get("statusCode"),
                            firecrawl_version="cli-1.19.27",
                            crawl_options={
                                "operation": "search --scrape replay",
                                "formats": ["markdown"],
                            },
                            metadata=metadata,
                        ),
                        "metadata": metadata,
                    }
                )
            else:
                requests.append(
                    {
                        "requested_url": url or "unknown:",
                        "error": "Firecrawl candidate has no scraped markdown",
                        "metadata": metadata,
                    }
                )
    context["raw_ingest_requests"] = requests
    return requests


__all__ = [
    "NETWORK_ENTRY_STATES",
    "PLANNING_STATES",
    "TERMINAL_STATES",
    "SmartResumeError",
    "coverage_context",
    "replay_extraction_inputs",
]
