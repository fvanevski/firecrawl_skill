"""ARC-17 Qdrant safety integration tests.

These tests may mutate Qdrant only when an explicitly disposable endpoint has
been authorized with both RESEARCH_STORE_TEST_QDRANT_URL and
RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET. They never use the host QDRANT_URL as
implicit scratch state and never manipulate the production alias name.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from qdrant_test_support import delete_alias, require_disposable_qdrant_url
from research_store.config import StoreConfig
from research_store.qdrant import QdrantIndex

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
TEST_QDRANT_URL = os.environ.get("RESEARCH_STORE_TEST_QDRANT_URL")
TEST_QDRANT_ALLOW_RESET = os.environ.get("RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET")
HOST_ALIAS = os.environ.get("QDRANT_ALIAS", "research_chunks_active")
pytestmark = pytest.mark.skipif(
    not TEST_DSN
    or not TEST_QDRANT_URL
    or (TEST_QDRANT_ALLOW_RESET or "").rstrip("/") != TEST_QDRANT_URL.rstrip("/"),
    reason="requires explicitly authorized disposable PostgreSQL and Qdrant endpoints",
)


def _make_config(tmp_path: Path, *, dimension: int = 4) -> StoreConfig:
    """Build a test config that cannot inherit the host production alias."""
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        qdrant_url=require_disposable_qdrant_url(),
        qdrant_alias=f"arc17_test_alias_{uuid4().hex}",
        blob_root=tmp_path / "blobs",
        embedding_model=f"arc17-test-{uuid4().hex}",
        embedding_revision="main",
        embedding_dimension=dimension,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_name="structural",
        chunker_version="structural-v1",
    )


def _create_collection(config: StoreConfig, name: str, dimension: int | None = None):
    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        name,
        dimension or config.embedding_dimension,
    )
    index._request(
        "PUT",
        f"/collections/{name}",
        {
            "vectors": {
                "dense": {
                    "size": dimension or config.embedding_dimension,
                    "distance": "Cosine",
                }
            }
        },
    )
    return index


def _cleanup(index: QdrantIndex, alias: str | None = None):
    if alias:
        with suppress(Exception):
            delete_alias(index, alias)
    with suppress(Exception):
        if index.inspect_schema().get("exists"):
            index.delete_collection()


def test_arc17_config_never_inherits_host_alias(tmp_path):
    config = _make_config(tmp_path)
    assert config.qdrant_url.rstrip("/") == require_disposable_qdrant_url()
    assert config.qdrant_alias.startswith("arc17_test_alias_")
    assert config.qdrant_alias != HOST_ALIAS


def test_switch_alias_supports_explicit_lifecycle_on_disposable_alias(tmp_path):
    config = _make_config(tmp_path)
    old_name = f"arc17_old_{uuid4().hex}"
    new_name = f"arc17_new_{uuid4().hex}"
    old_index = _create_collection(config, old_name)
    new_index = _create_collection(config, new_name)
    try:
        assert old_index.switch_alias(config.qdrant_alias, old_name) is True
        assert old_index.switch_alias(config.qdrant_alias, new_name) is True
        assert old_index.list_aliases()[config.qdrant_alias] == new_name
        assert old_index.switch_alias(config.qdrant_alias, new_name) is False
    finally:
        _cleanup(old_index, config.qdrant_alias)
        _cleanup(new_index, config.qdrant_alias)


def test_require_compatible_schema_is_non_destructive_on_missing_collection(tmp_path):
    config = _make_config(tmp_path)
    name = f"arc17_missing_{uuid4().hex}"
    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        name,
        config.embedding_dimension,
    )
    with pytest.raises(RuntimeError, match="missing"):
        index.require_compatible_schema()
    assert index.inspect_schema()["exists"] is False


def test_require_compatible_schema_is_non_destructive_on_incompatible_collection(tmp_path):
    config = _make_config(tmp_path, dimension=8)
    name = f"arc17_incompatible_{uuid4().hex}"
    created = _create_collection(config, name, dimension=4)
    requested = QdrantIndex(config.qdrant_url, config.qdrant_api_key, name, 8)
    try:
        with pytest.raises(RuntimeError, match="incompatible schema"):
            requested.require_compatible_schema()
        schema = created.inspect_schema()
        assert schema["exists"] is True
        assert schema["actual"]["size"] == 4
    finally:
        _cleanup(created)


def test_ensure_schema_refuses_to_recreate_any_aliased_collection(tmp_path):
    config = _make_config(tmp_path, dimension=8)
    name = f"arc17_aliased_{uuid4().hex}"
    created = _create_collection(config, name, dimension=4)
    requested = QdrantIndex(config.qdrant_url, config.qdrant_api_key, name, 8)
    try:
        created.switch_alias(config.qdrant_alias, name)
        with pytest.raises(RuntimeError, match="active alias"):
            requested.ensure_schema()
        aliases = requested.list_aliases()
        assert aliases[config.qdrant_alias] == name
        assert created.inspect_schema()["actual"]["size"] == 4
    finally:
        _cleanup(created, config.qdrant_alias)


def test_ensure_schema_may_rebuild_unaliased_disposable_collection(tmp_path):
    config = _make_config(tmp_path, dimension=8)
    name = f"arc17_unaliased_{uuid4().hex}"
    _create_collection(config, name, dimension=4)
    requested = QdrantIndex(config.qdrant_url, config.qdrant_api_key, name, 8)
    try:
        result = requested.ensure_schema()
        assert result["recreated"] is True
        assert requested.inspect_schema()["actual"]["size"] == 8
    finally:
        _cleanup(requested)


def test_owned_cleanup_does_not_enumerate_or_delete_unrelated_collection(tmp_path):
    """Resetting one owned projection leaves another disposable projection intact."""
    from qdrant_test_support import reset_disposable_projection

    config = _make_config(tmp_path)
    owned = config.physical_collection
    unrelated = f"arc17_unrelated_{uuid4().hex}"
    owned_index = _create_collection(config, owned)
    unrelated_index = _create_collection(config, unrelated)
    try:
        owned_index.switch_alias(config.qdrant_alias, owned)
        reset_disposable_projection(config)
        assert owned_index.inspect_schema()["exists"] is False
        assert unrelated_index.inspect_schema()["exists"] is True
        assert config.qdrant_alias not in unrelated_index.list_aliases()
    finally:
        _cleanup(owned_index, config.qdrant_alias)
        _cleanup(unrelated_index)
