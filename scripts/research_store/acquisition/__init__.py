"""Acquisition capability boundary.

Canonical acquisition code is organized into explicit submodules:

- ``authority``: fail-closed PostgreSQL/blob readiness policy
- ``models``: acquisition request/result/transport models
- ``ports``: provider-facing protocols
- ``service``: search acquisition application policy
- ``direct_scrape``: direct-scrape application/persistence policy
- ``adapters``: concrete Firecrawl/network transports

This package initializer deliberately imports none of those modules. Keeping the
root inert prevents repository/domain imports from acquiring PostgreSQL or
transport dependencies merely by importing ``research_store.acquisition`` and
makes dependency direction visible at each call site.
"""

__all__: list[str] = []
