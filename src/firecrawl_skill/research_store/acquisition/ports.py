"""Ports owned by the acquisition capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from .models import AcquisitionResult, ScrapeTransportResult, SearchAdapterResult


class SearchAdapter(Protocol):
    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        **kwargs: Any,
    ) -> SearchAdapterResult: ...


class AcquisitionExecutor(Protocol):
    """Execute an authoritative acquisition search through an injected service."""

    def execute_search(
        self,
        run_id: UUID,
        query_text: str,
        *,
        backend: str = "firecrawl",
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        parent_invocation_id: UUID | None = None,
        idempotency_key: str | None = None,
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        metadata: dict[str, Any] | None = None,
        authority_context: Any | None = None,
        replay_existing: bool = True,
    ) -> AcquisitionResult: ...


class CandidateScrapeAdapter(Protocol):
    """Bounded candidate extraction used after discovery."""

    def scrape_url(
        self,
        url: str,
        *,
        transient_retries: int | None = None,
    ) -> SearchAdapterResult: ...


class DirectScrapeAdapter(Protocol):
    def scrape(
        self,
        url: str,
        *,
        format: str = "markdown",
        summary: bool = False,
        schema: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ScrapeTransportResult: ...


__all__ = [
    "AcquisitionExecutor",
    "CandidateScrapeAdapter",
    "DirectScrapeAdapter",
    "SearchAdapter",
]
