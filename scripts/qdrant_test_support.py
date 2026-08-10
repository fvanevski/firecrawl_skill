"""Pytest support for explicitly disposable Qdrant integration environments.

A PostgreSQL test DSN does not imply that the configured Qdrant endpoint is
safe to reset. Qdrant-mutating integration tests must opt in to an exact
separate endpoint through RESEARCH_STORE_TEST_QDRANT_URL and repeat that exact
URL in RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET. This mirrors the database reset
contract and prevents a host production Qdrant from being treated as scratch
state merely because tests are running.
"""

from __future__ import annotations

import os
from contextlib import suppress
from uuid import uuid4

import pytest
from research_store.config import StoreConfig
from research_store.qdrant import QdrantIndex


def require_disposable_qdrant_url() -> str:
    """Return the exact authorized disposable Qdrant URL or fail closed."""
    url = os.environ.get("RESEARCH_STORE_TEST_QDRANT_URL", "").rstrip("/")
    allow_reset = os.environ.get("RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET", "").rstrip(
        "/"
    )
    if not url:
        raise RuntimeError(
            "Qdrant integration tests require RESEARCH_STORE_TEST_QDRANT_URL; "
            "never reuse the host QDRANT_URL implicitly"
        )
    if allow_reset != url:
        raise RuntimeError(
            "RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET must exactly equal "
            "RESEARCH_STORE_TEST_QDRANT_URL"
        )
    return url


def delete_alias(index: QdrantIndex, alias: str) -> None:
    """Delete one exact alias if present; never enumerate/delete other aliases."""
    if alias not in index.list_aliases():
        return
    index._request(
        "POST",
        "/collections/aliases",
        {"actions": [{"delete_alias": {"alias_name": alias}}]},
    )


def reset_disposable_projection(config: StoreConfig) -> None:
    """Reset only the configured test alias and physical collection.

    The function refuses to operate unless the config points at the exact
    explicitly authorized disposable endpoint. It never enumerates collections
    and therefore cannot delete unrelated inactive/candidate projections.
    """
    disposable_url = require_disposable_qdrant_url()
    if config.qdrant_url.rstrip("/") != disposable_url:
        raise RuntimeError(
            f"refusing Qdrant reset for {config.qdrant_url!r}; "
            f"authorized disposable endpoint is {disposable_url!r}"
        )

    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.physical_collection,
        config.embedding_dimension,
    )
    delete_alias(index, config.qdrant_alias)
    if index.inspect_schema().get("exists"):
        index.delete_collection()


@pytest.fixture(scope="session")
def disposable_qdrant_url() -> str:
    try:
        return require_disposable_qdrant_url()
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session", autouse=True)
def isolate_qdrant_test_environment():
    """Redirect integration tests only when an explicit disposable URL is supplied."""
    if not os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL"):
        yield
        return
    if not os.environ.get("RESEARCH_STORE_TEST_QDRANT_URL"):
        yield
        return

    url = require_disposable_qdrant_url()
    original_url = os.environ.get("QDRANT_URL")
    original_alias = os.environ.get("QDRANT_ALIAS")
    test_alias = f"firecrawl_test_{uuid4().hex}"
    os.environ["QDRANT_URL"] = url
    os.environ["QDRANT_ALIAS"] = test_alias
    try:
        yield
    finally:
        cleanup = QdrantIndex(
            url,
            os.environ.get("QDRANT_API_KEY", ""),
            "__test_alias_cleanup__",
            1,
        )
        with suppress(Exception):
            delete_alias(cleanup, test_alias)
        if original_url is None:
            os.environ.pop("QDRANT_URL", None)
        else:
            os.environ["QDRANT_URL"] = original_url
        if original_alias is None:
            os.environ.pop("QDRANT_ALIAS", None)
        else:
            os.environ["QDRANT_ALIAS"] = original_alias


@pytest.fixture(scope="session")
def track_test_collection() -> set[str]:
    """Return the session-owned physical-collection registry."""
    return set()


@pytest.fixture(scope="session", autouse=True)
def cleanup_owned_qdrant_collections(track_test_collection: set[str]):
    """Delete only collections explicitly registered as test-owned."""
    yield
    if not track_test_collection or not os.environ.get("RESEARCH_STORE_TEST_QDRANT_URL"):
        return
    try:
        url = require_disposable_qdrant_url()
    except RuntimeError:
        return
    cleanup = QdrantIndex(
        url,
        os.environ.get("QDRANT_API_KEY", ""),
        "__test_collection_cleanup__",
        1,
    )
    for collection in sorted(track_test_collection):
        index = cleanup.for_collection(collection, 1)
        with suppress(Exception):
            aliases = index.list_aliases()
            for alias, target in aliases.items():
                if target == collection and alias.startswith("firecrawl_test_"):
                    delete_alias(index, alias)
            if index.inspect_schema().get("exists"):
                index.delete_collection()
