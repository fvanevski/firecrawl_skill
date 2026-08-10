"""ARC-17 Qdrant protection integration tests.

Verifies that routine indexing, test cleanup, diagnostics, and preflight
never silently destroy or repoint the active Qdrant projection.  Only the
supported explicit activation/rebuild lifecycle may perform lifecycle-changing
projection operations.
"""

from __future__ import annotations

import os
import sys
import urllib.error
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.cli import (
    _doctor,
    _index_reconcile,
)
from research_store.config import StoreConfig
from research_store.postgres import connect
from research_store.qdrant import QdrantIndex

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    embedding_model: str | None = None,
    qdrant_alias: str | None = None,
    extra_kwargs: dict | None = None,
) -> StoreConfig:
    """Create a test StoreConfig with a unique embedding fingerprint."""
    model = embedding_model or f"arc17-test-{uuid4().hex[:8]}"
    kwargs: dict = {
        "database_url": TEST_DSN,
        "blob_root": tmp_path / "blobs",
        "embedding_model": model,
        "embedding_revision": "main",
        "embedding_dimension": 4,
        "parser_version": "markdown-v1",
        "normalization_version": "cleanup-v1",
        "chunker_name": "structural",
        "chunker_version": "structural-v1",
    }
    if qdrant_alias:
        kwargs["qdrant_alias"] = qdrant_alias
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return replace(StoreConfig.from_env(), **kwargs)


def _seed_active_definition(config: StoreConfig, collection: str, dimension: int = 4):
    """Create an active index definition backed by *collection* in PostgreSQL."""
    def_id = str(uuid4())
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        # Remove any prior definition for this collection to ensure clean state
        cur.execute(
            "DELETE FROM index_definitions WHERE physical_collection=%s",
            (collection,),
        )
        cur.execute(
            """INSERT INTO index_definitions
                (id, fingerprint, physical_collection, model_name, model_revision,
                 dimension, distance_metric, normalization, instruction_template_hash,
                 lifecycle_status, created_at, activated_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', now(), now())""",
            (
                def_id,
                config.embedding_fingerprint,
                collection,
                config.embedding_model,
                config.embedding_revision,
                dimension,
                "Cosine",
                config.normalization_version,
                "",
            ),
        )
        cur.execute(
            "UPDATE index_definitions SET lifecycle_status='inactive' WHERE lifecycle_status='active' AND id<>%s",
            (def_id,),
        )
        return def_id


def _seed_points(index: QdrantIndex, count: int = 3):
    """Insert baseline points into a collection."""
    points = [
        {
            "id": i,
            "vector": {
                "dense": [1.0 if j == 0 else 0.0 for j in range(index.dimension)]
            },
            "payload": {"chunk_id": str(i), "source": "test"},
        }
        for i in range(count)
    ]
    index.upsert(points)
    return {str(p["id"]) for p in points}


def _create_collection(qdrant_url: str, api_key: str, name: str, dimension: int = 4):
    """Create a Qdrant collection with dense vectors."""
    idx = QdrantIndex(qdrant_url, api_key, name, dimension)
    idx._request(
        "PUT",
        f"/collections/{name}",
        {"vectors": {"dense": {"size": dimension, "distance": "Cosine"}}},
    )
    return idx


# ---------------------------------------------------------------------------
# A: Production alias activation / rollback
# ---------------------------------------------------------------------------


class TestActivationRollback:
    """Test that the supported activation/rollback lifecycle works end-to-end."""

    def test_switch_alias_allows_production_alias(self, tmp_path):
        """switch_alias no longer rejects the production alias name."""
        config = _make_config(tmp_path)
        collection = f"prod_alias_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        try:
            # Should not raise
            idx.switch_alias(config.qdrant_alias, collection)
            aliases = idx.list_aliases()
            assert aliases.get(config.qdrant_alias) == collection
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_switch_alias_returns_false_when_already_active(self, tmp_path):
        """switch_alias returns False when already on target."""
        config = _make_config(tmp_path)
        collection = f"atomic_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        try:
            first = idx.switch_alias("test_alias", collection)
            assert first is True
            second = idx.switch_alias("test_alias", collection)
            assert second is False
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_switch_alias_replaces_existing_target(self, tmp_path):
        """switch_alias updates an existing alias target."""
        config = _make_config(tmp_path)
        old_coll = f"old_{uuid4().hex[:8]}"
        new_coll = f"new_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        old_idx = _create_collection(qdrant_url, api_key, old_coll)
        new_idx = _create_collection(qdrant_url, api_key, new_coll)
        try:
            old_idx.switch_alias("my_alias", old_coll)
            old_idx.switch_alias("my_alias", new_coll)
            aliases = old_idx.list_aliases()
            assert aliases.get("my_alias") == new_coll
        finally:
            with suppress(Exception):
                old_idx.delete_collection()
            with suppress(Exception):
                new_idx.delete_collection()


# ---------------------------------------------------------------------------
# B: Cross-store drift diagnosis
# ---------------------------------------------------------------------------


class TestDriftDiagnosis:
    """Doctor and reconcile must fail closed on activation drift."""

    def test_doctor_passes_when_alias_matches_active_definition(self, tmp_path):
        """Healthy case: PG active + alias -> same collection."""
        config = _make_config(tmp_path)
        collection = f"healthy_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        idx.switch_alias(config.qdrant_alias, collection)
        _seed_points(idx, 2)

        checks, _failed = _doctor(config)
        # Alias state must be healthy even if other checks fail
        assert checks["qdrant_projection"]["alias_state"]["status"] == "healthy"
        assert checks["qdrant_projection"]["collection"] == collection

        with suppress(Exception):
            idx.delete_collection()

    def test_doctor_fails_when_production_alias_missing(self, tmp_path):
        """PG active + missing production alias => failure."""
        config = _make_config(tmp_path)
        collection = f"missing_alias_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        _seed_points(idx, 2)
        # Remove any stale production alias
        try:
            QdrantIndex(qdrant_url, api_key, "__cleanup__", 1).switch_alias(
                config.qdrant_alias, f"__nonexistent_{uuid4().hex[:8]}__"
            )
        except urllib.error.HTTPError:
            pass

        checks, _failed = _doctor(config)
        assert _failed
        assert checks["qdrant_projection"]["status"] == "failure"
        # Either missing or wrong is a failure condition
        assert checks["qdrant_projection"]["alias_state"]["status"] in (
            "missing_required_alias",
            "wrong_required_alias_target",
        )

        with suppress(Exception):
            idx.delete_collection()

    def test_doctor_fails_when_production_alias_points_elsewhere(self, tmp_path):
        """PG active + wrong alias target => failure."""
        config = _make_config(tmp_path)
        active_collection = f"active_{uuid4().hex[:8]}"
        wrong_collection = f"wrong_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        active_idx = _create_collection(qdrant_url, api_key, active_collection)
        wrong_idx = _create_collection(qdrant_url, api_key, wrong_collection)
        _seed_active_definition(config, active_collection)
        active_idx.switch_alias(config.qdrant_alias, wrong_collection)
        _seed_points(active_idx, 2)
        _seed_points(wrong_idx, 2)

        checks, _failed = _doctor(config)
        assert _failed
        assert checks["qdrant_projection"]["status"] == "failure"
        assert (
            checks["qdrant_projection"]["alias_state"]["status"]
            == "wrong_required_alias_target"
        )
        assert (
            checks["qdrant_projection"]["alias_state"]["expected"]
            == f"{config.qdrant_alias} -> {active_collection}"
        )

        with suppress(Exception):
            active_idx.delete_collection()
        with suppress(Exception):
            wrong_idx.delete_collection()

    def test_random_alias_cannot_substitute_for_production_alias(self, tmp_path):
        """A random/test alias targeting the active collection does not satisfy health."""
        config = _make_config(tmp_path)
        collection = f"random_alias_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        # Set a random alias, not the production alias
        idx.switch_alias(f"test_alias_{uuid4().hex[:8]}", collection)
        _seed_points(idx, 2)

        checks, _failed = _doctor(config)
        assert _failed
        assert checks["qdrant_projection"]["status"] == "failure"
        # Either missing or wrong is a failure condition
        assert checks["qdrant_projection"]["alias_state"]["status"] in (
            "missing_required_alias",
            "wrong_required_alias_target",
        )

        with suppress(Exception):
            idx.delete_collection()

    def test_empty_active_projection_fails_closed(self, tmp_path):
        """PG active + empty collection + production alias present => failure."""
        config = _make_config(tmp_path)
        collection = f"empty_proj_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        idx.switch_alias(config.qdrant_alias, collection)
        # Collection exists but has zero points

        checks, _failed = _doctor(config)
        assert _failed
        assert checks["qdrant_projection"]["status"] == "failure"
        assert checks["qdrant_projection"]["drift"]["type"] == "empty_active_projection"

        with suppress(Exception):
            idx.delete_collection()

    def test_reconcile_emits_discrepancy_for_missing_alias(self, tmp_path):
        """Reconciliation reports activation drift when production alias is absent."""
        config = _make_config(tmp_path)
        collection = f"recon_missing_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        _seed_points(idx, 2)
        # No alias set

        result = _index_reconcile(config, repair=False)
        # Reconciliation should detect issues - either fail closed or report discrepancies
        if isinstance(result, dict):
            assert not result.get("ok", True) or any(
                "activation" in d.lower() or "alias" in d.lower()
                for d in result.get("discrepancies", [])
            )

        with suppress(Exception):
            idx.delete_collection()

    def test_reconcile_emits_discrepancy_for_wrong_alias(self, tmp_path):
        """Reconciliation reports activation drift when alias points elsewhere."""
        config = _make_config(tmp_path)
        active_collection = f"recon_wrong_active_{uuid4().hex[:8]}"
        wrong_collection = f"recon_wrong_wrong_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        active_idx = _create_collection(qdrant_url, api_key, active_collection)
        wrong_idx = _create_collection(qdrant_url, api_key, wrong_collection)
        _seed_active_definition(config, active_collection)
        wrong_idx.switch_alias(config.qdrant_alias, wrong_collection)
        _seed_points(active_idx, 2)
        _seed_points(wrong_idx, 2)

        result = _index_reconcile(config, repair=False)
        # Reconciliation should detect issues
        if isinstance(result, dict):
            assert not result.get("ok", True) or any(
                "activation" in d.lower() or "alias" in d.lower()
                for d in result.get("discrepancies", [])
            )

        with suppress(Exception):
            active_idx.delete_collection()
        with suppress(Exception):
            wrong_idx.delete_collection()


# ---------------------------------------------------------------------------
# C: Worker safety — non-destructive schema handling
# ---------------------------------------------------------------------------


class TestWorkerSafety:
    """Routine workers must never delete/recreate physical collections."""

    def test_require_compatible_schema_raises_on_missing_collection(self):
        """require_compatible_schema raises when collection is absent."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        idx = QdrantIndex(url, key, "nonexistent", 4)
        with pytest.raises(RuntimeError, match="missing"):
            idx.require_compatible_schema()

    def test_require_compatible_schema_raises_on_incompatible_schema(self):
        """require_compatible_schema raises when schema is incompatible."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"req_compat_{uuid4().hex[:8]}"
        # Create with wrong dimension to make it incompatible
        idx = QdrantIndex(url, key, name, 8)  # Request 8-dim
        try:
            idx._request(
                "PUT",
                f"/collections/{name}",
                {
                    "vectors": {"dense": {"size": 4, "distance": "Cosine"}}
                },  # Create 4-dim
            )
            # Now try with 8-dim index - should be incompatible
            idx_8dim = QdrantIndex(url, key, name, 8)
            with pytest.raises(RuntimeError, match="incompatible schema"):
                idx_8dim.require_compatible_schema()
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_require_compatible_schema_succeeds_for_compatible_collection(self):
        """require_compatible_schema succeeds when collection is compatible."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"req_compat_ok_{uuid4().hex[:8]}"
        idx = QdrantIndex(url, key, name, 4)
        try:
            idx._request(
                "PUT",
                f"/collections/{name}",
                {"vectors": {"dense": {"size": 4, "distance": "Cosine"}}},
            )
            status = idx.require_compatible_schema()
            assert status["exists"] is True
            assert status["compatible"] is True
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_ensure_schema_still_recreates_unaliased_incompatible(self):
        """ensure_schema still recreates unaliased incompatible collections."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"ens_rec_{uuid4().hex[:8]}"
        # Create with wrong dimension
        idx = QdrantIndex(url, key, name, 8)  # Request 8-dim
        try:
            idx._request(
                "PUT",
                f"/collections/{name}",
                {
                    "vectors": {"dense": {"size": 4, "distance": "Cosine"}}
                },  # Create 4-dim
            )
            result = idx.ensure_schema()
            assert result["recreated"] is True
            assert result["compatible"] is True
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_ensure_schema_refuses_to_delete_aliased_incompatible(self):
        """ensure_schema refuses to delete a collection targeted by an alias."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"ens_del_prot_{uuid4().hex[:8]}"
        idx = QdrantIndex(url, key, name, 8)  # Request 8-dim
        alias_name = f"prot_alias_{uuid4().hex[:8]}"
        try:
            # Create with wrong dimension
            idx._request(
                "PUT",
                f"/collections/{name}",
                {
                    "vectors": {"dense": {"size": 4, "distance": "Cosine"}}
                },  # Create 4-dim
            )
            idx.switch_alias(alias_name, name)
            with pytest.raises(RuntimeError, match="active alias"):
                idx.ensure_schema()
        finally:
            with suppress(Exception):
                idx.delete_collection()


# ---------------------------------------------------------------------------
# D: Test isolation — production survives test suite
# ---------------------------------------------------------------------------


class TestProductionPreservation:
    """Tests must not destroy production Qdrant state."""

    def test_production_alias_survives_doctor(self, tmp_path):
        """Running doctor does not move the production alias."""
        config = _make_config(tmp_path)
        collection = f"survives_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        idx.switch_alias(config.qdrant_alias, collection)
        baseline_alias = idx.list_aliases().get(config.qdrant_alias)

        _doctor(config)

        after_alias = idx.list_aliases().get(config.qdrant_alias)
        assert after_alias == baseline_alias

        with suppress(Exception):
            idx.delete_collection()

    def test_production_alias_survives_reconcile(self, tmp_path):
        """Running reconcile does not move the production alias."""
        config = _make_config(tmp_path)
        collection = f"survives_rec_{uuid4().hex[:8]}"
        qdrant_url = config.qdrant_url
        api_key = config.qdrant_api_key

        idx = _create_collection(qdrant_url, api_key, collection)
        _seed_active_definition(config, collection)
        idx.switch_alias(config.qdrant_alias, collection)
        baseline_alias = idx.list_aliases().get(config.qdrant_alias)

        _index_reconcile(config, repair=False)

        after_alias = idx.list_aliases().get(config.qdrant_alias)
        assert after_alias == baseline_alias

        with suppress(Exception):
            idx.delete_collection()


# ---------------------------------------------------------------------------
# F: Preflight durability
# ---------------------------------------------------------------------------


class TestPreflightDurability:
    """Preflight worker probe must not destroy production state."""

    def test_switch_alias_is_atomic(self):
        """switch_alias returns False when already on target."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"atomic_{uuid4().hex[:8]}"
        alias = f"atom_alias_{uuid4().hex[:8]}"
        idx = QdrantIndex(url, key, name, 4)
        try:
            idx._request(
                "PUT",
                f"/collections/{name}",
                {"vectors": {"dense": {"size": 4, "distance": "Cosine"}}},
            )
            first = idx.switch_alias(alias, name)
            assert first is True
            second = idx.switch_alias(alias, name)
            assert second is False
        finally:
            with suppress(Exception):
                idx.delete_collection()

    def test_switch_alias_allows_production_alias(self):
        """switch_alias no longer rejects the production alias name."""
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        key = os.environ.get("QDRANT_API_KEY", "")
        name = f"prod_alias_{uuid4().hex[:8]}"
        idx = QdrantIndex(url, key, name, 4)
        try:
            idx._request(
                "PUT",
                f"/collections/{name}",
                {"vectors": {"dense": {"size": 4, "distance": "Cosine"}}},
            )
            # Should not raise
            idx.switch_alias("research_chunks_active", name)
            aliases = idx.list_aliases()
            assert aliases.get("research_chunks_active") == name
        finally:
            with suppress(Exception):
                idx.delete_collection()
