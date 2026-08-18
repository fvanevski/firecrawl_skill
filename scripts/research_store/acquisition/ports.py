"""Ports owned by the acquisition capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import ScrapeTransportResult, SearchAdapterResult


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


__all__ = ["DirectScrapeAdapter", "SearchAdapter"]
