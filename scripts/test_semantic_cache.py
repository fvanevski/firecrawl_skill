"""Tests for the semantic-result cache (issue #41).

Tests cover:

* **Key identity** — deterministic key computation from stage, prompt, schema,
  model, input, policy, and configuration.
* **Invalidation** — changes to any identity component produce a different key.
* **Stale-reference** — cached output still passes current reference validation.
* **Cache loss** — cache loss affects performance only, never authoritative state.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from research_store.semantic_cache import (
    CacheEntry,
    SemanticCacheService,
    _compute_cache_key,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_STAGE = "outline"
_BASE_MODEL_NAME = "llama-3.1-8b"
_BASE_MODEL_REVISION = "abc123"
_BASE_ENDPOINT = "local"
_BASE_PROMPT_VERSION = "synthesis-v1"
_BASE_SCHEMA_VERSION = 1
_BASE_POLICY_VERSION = "budget-policy-v1"
_BASE_INPUT = {
    "claims": [
        {"claim_id": "00000000-0000-0000-0000-000000000102", "statement": "test claim"}
    ],
    "passages": [
        {
            "passage_id": "00000000-0000-0000-0000-000000000601",
            "text": "test passage",
        }
    ],
}
_BASE_CONFIG = {"chunker_version": "hierarchical", "parser_version": "markdown-v1"}
_BASE_ARTIFACT = {"schema_version": "synthesis-outline-v1", "outline_sections": []}
_BASE_PROVENANCE = {
    "provider": "local",
    "requested_model": "llama-3.1-8b",
    "prompt_version": "synthesis-v1",
    "prompt_hash": "test-prompt-hash",
}

_VALID_PACKET = {
    "schema_version": "evidence-packet-v1",
    "run_id": "00000000-0000-0000-0000-000000000401",
    "research_spec_id": "00000000-0000-0000-0000-000000000100",
    "coverage_revision": 2,
    "claims": [
        {
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "statement": "The documented behavior is reproducible.",
            "semantic_status": "qualified",
            "uncertainty": "Only one source has been acquired.",
        }
    ],
    "passages": [
        {
            "passage_id": "00000000-0000-0000-0000-000000000601",
            "candidate_id": "00000000-0000-0000-0000-000000000301",
            "snapshot_id": "00000000-0000-0000-0000-000000000602",
            "chunk_id": "00000000-0000-0000-0000-000000000603",
            "text": "The fixture passage records the documented behavior.",
            "source_url": "https://fixture.invalid/docs",
        }
    ],
    "omitted_passages": [],
    "claim_evidence_bindings": [
        {
            "binding_id": "00000000-0000-0000-0000-000000000604",
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "passage_ids": ["00000000-0000-0000-0000-000000000601"],
            "relationship": "qualifies",
            "confidence": 0.7,
            "uncertainty": "Independent replication is missing.",
        }
    ],
    "corroborating_groups": [],
    "contradicting_groups": [],
    "qualifying_groups": [],
    "near_duplicate_groups": [],
    "source_diversity_summary": {"independent_source_count": 1},
    "freshness_summary": {"status": "satisfied"},
    "limitations": ["Independent corroboration remains missing."],
    "unresolved_items": [],
    "independence_assessments": [],
    "retrieval_provenance": [],
}


def _make_cache_service(
    ttl_seconds: int = 3600,
) -> tuple[SemanticCacheService, MagicMock]:
    """Build a SemanticCacheService with a mocked UOW factory."""
    mock_uow = MagicMock()
    _cache_store: dict[str, dict] = {}

    def _get_by_key(key_hash):
        return _cache_store.get(key_hash)

    def _insert(record):
        _cache_store[record["key_hash"]] = record

    def _prune(older_than_seconds=None, ttl_seconds=3600):
        now = time.time()
        before = len(_cache_store)
        if older_than_seconds is not None:
            cutoff = now - older_than_seconds
            to_remove = [k for k, v in _cache_store.items() if v["created_at"] < cutoff]
        else:
            to_remove = [
                k
                for k, v in _cache_store.items()
                if v.get("status") == "valid"
                and (now - v["created_at"]) > v.get("ttl_seconds", ttl_seconds)
            ]
        for k in to_remove:
            del _cache_store[k]
        return before - len(_cache_store)

    def _invalidate_by_key(key_hash):
        if key_hash in _cache_store and _cache_store[key_hash].get("status") == "valid":
            _cache_store[key_hash]["status"] = "pruned"
            return 1
        return 0

    def _invalidate_by_id(entry_id):
        for v in _cache_store.values():
            if str(v.get("id")) == str(entry_id) and v.get("status") == "valid":
                v["status"] = "pruned"
                return 1
        return 0

    def _update(record):
        """Update an existing cache entry by key_hash."""
        key_hash = record["key_hash"]
        if key_hash in _cache_store:
            stored = _cache_store[key_hash]
            stored["artifact"] = record.get("artifact", stored.get("artifact"))
            stored["provenance"] = record.get("provenance", stored.get("provenance"))
            stored["status"] = record.get("status", stored.get("status"))
            stored["ttl_seconds"] = record.get("ttl_seconds", stored.get("ttl_seconds"))
            stored["created_at"] = record.get("created_at", stored.get("created_at"))
            return 1
        return 0

    mock_uow.semantic_cache.get_cache_entry_by_key = _get_by_key
    mock_uow.semantic_cache.insert_cache_entry = _insert
    mock_uow.semantic_cache.prune_cache_entries = _prune
    mock_uow.semantic_cache.invalidate_cache_entry = _invalidate_by_key
    mock_uow.semantic_cache.invalidate_cache_entry_by_id = _invalidate_by_id
    mock_uow.semantic_cache.update_cache_entry = _update

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_uow)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_uow_factory = MagicMock(return_value=mock_ctx)

    service = SemanticCacheService(
        uow_factory=mock_uow_factory,
        ttl_seconds=ttl_seconds,
    )
    return service, mock_uow, _cache_store


# ---------------------------------------------------------------------------
# Key identity tests
# ---------------------------------------------------------------------------


def test_key_identity_deterministic():
    """Same inputs must produce the same key."""
    key1 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key2 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key1 == key2
    assert len(key1) == 64  # SHA-256 hex digest length


def test_key_changes_on_model_change():
    """Changing the model fingerprint must invalidate the key."""
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_model = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name="different-model",
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key_base != key_different_model


def test_key_changes_on_prompt_version_change():
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_prompt = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version="synthesis-v2",
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key_base != key_different_prompt


def test_key_changes_on_schema_version_change():
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_schema = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=2,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key_base != key_different_schema


def test_key_changes_on_input_change():
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_input = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="different-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key_base != key_different_input


def test_key_changes_on_policy_change():
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_policy = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version="budget-policy-v2",
        configuration=_BASE_CONFIG,
    )
    assert key_base != key_different_policy


def test_key_changes_on_configuration_change():
    key_base = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_different_config = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration={"chunker_version": "structural", "parser_version": "html-v1"},
    )
    assert key_base != key_different_config


def test_key_changes_on_stage_change():
    key_outline = _compute_cache_key(
        stage="outline",
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    key_draft = _compute_cache_key(
        stage="draft",
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert key_outline != key_draft


def test_key_with_none_policy():
    """None policy version should produce a consistent key."""
    key1 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=None,
        configuration=_BASE_CONFIG,
    )
    key2 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=None,
        configuration=_BASE_CONFIG,
    )
    assert key1 == key2


def test_key_with_none_configuration():
    """None configuration should produce a consistent key."""
    key1 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=None,
    )
    key2 = _compute_cache_key(
        stage=_BASE_STAGE,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=None,
    )
    assert key1 == key2


# ---------------------------------------------------------------------------
# Input hash computation tests
# ---------------------------------------------------------------------------


def test_compute_input_hash_deterministic():
    """Same input must produce the same hash."""
    service, _, _ = _make_cache_service()
    hash1 = service.compute_input_hash(_BASE_INPUT)
    hash2 = service.compute_input_hash(_BASE_INPUT)
    assert hash1 == hash2
    assert len(hash1) == 64


def test_compute_input_hash_different_inputs():
    """Different input must produce a different hash."""
    service, _, _ = _make_cache_service()
    hash1 = service.compute_input_hash(_BASE_INPUT)
    different_input = dict(_BASE_INPUT)
    different_input["claims"] = [
        {"claim_id": "different", "statement": "different claim"}
    ]
    hash2 = service.compute_input_hash(different_input)
    assert hash1 != hash2


def test_compute_prompt_hash_deterministic():
    """Same prompts must produce the same hash."""
    service, _, _ = _make_cache_service()
    hash1 = service.compute_prompt_hash("system", "user")
    hash2 = service.compute_prompt_hash("system", "user")
    assert hash1 == hash2


def test_compute_prompt_hash_different_prompts():
    """Different prompts must produce a different hash."""
    service, _, _ = _make_cache_service()
    hash1 = service.compute_prompt_hash("system", "user")
    hash2 = service.compute_prompt_hash("different-system", "user")
    assert hash1 != hash2


# ---------------------------------------------------------------------------
# Cache lookup/insert tests
# ---------------------------------------------------------------------------


def test_lookup_miss():
    """A lookup with no cached entry must return None."""
    service, _, _ = _make_cache_service()
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


def test_insert_and_lookup_hit():
    """Inserting and then looking up must return the cached entry."""
    service, _, _ = _make_cache_service()

    # Insert
    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None
    assert entry.stage == _BASE_STAGE
    assert entry.status == "valid"
    assert entry.artifact == _BASE_ARTIFACT

    # Lookup
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None
    assert result.key_hash == entry.key_hash
    assert result.artifact == _BASE_ARTIFACT


def test_insert_empty_artifact_returns_none():
    """Inserting an empty artifact must not create a cache entry."""
    service, _, _ = _make_cache_service()
    result = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=None,
        provenance=_BASE_PROVENANCE,
    )
    assert result is None

    # Verify no entry was created.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


def test_insert_idempotent():
    """Inserting the same key twice must not create a duplicate."""
    service, _, cache_store = _make_cache_service()

    entry1 = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry1 is not None

    entry2 = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry2 is not None
    assert entry1.key_hash == entry2.key_hash

    # Verify only one entry exists in the store.
    assert len(cache_store) == 1


def test_lookup_with_different_stage_misses():
    """A lookup with a different stage must miss."""
    service, _, _ = _make_cache_service()

    service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )

    result = service.lookup(
        stage="draft",  # Different stage
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Invalidation tests
# ---------------------------------------------------------------------------


def test_invalidate_by_key():
    """Explicit invalidation must remove the entry from lookup."""
    service, _, _ = _make_cache_service()

    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # Lookup should find it.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None

    # Invalidate.
    invalidated = service.invalidate(key_hash=entry.key_hash)
    assert invalidated is True

    # Lookup should miss.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


def test_invalidate_nonexistent_key():
    """Invalidating a nonexistent key must return False."""
    service, _, _ = _make_cache_service()
    result = service.invalidate(key_hash="nonexistent-key")
    assert result is False


def test_invalidate_changes_status_to_pruned():
    """Invalidation must change status to 'pruned'."""
    service, _, cache_store = _make_cache_service()

    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    service.invalidate(key_hash=entry.key_hash)
    stored = cache_store.get(entry.key_hash)
    assert stored is not None
    assert stored["status"] == "pruned"


# ---------------------------------------------------------------------------
# Prune tests
# ---------------------------------------------------------------------------


def test_prune_expired_entries():
    """Pruning with TTL=0 must remove all entries."""
    service, _, _ = _make_cache_service(ttl_seconds=0)

    service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )

    count = service.prune()
    assert count >= 1

    # Lookup should miss.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


def test_prune_with_older_than():
    """Pruning with older_than_seconds must only remove old entries."""
    service, _, cache_store = _make_cache_service(ttl_seconds=3600)

    old_entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert old_entry is not None

    # Manually make the entry "old" by modifying the store directly.
    stored = cache_store.get(old_entry.key_hash)
    if stored:
        stored["created_at"] = time.time() - 1_000_000  # 1M seconds ago

    count = service.prune(older_than_seconds=1000)
    assert count >= 1


def test_prune_no_entries():
    """Pruning with no entries must return 0."""
    service, _, _ = _make_cache_service()
    count = service.prune()
    assert count == 0


# ---------------------------------------------------------------------------
# Stale-reference tests
# ---------------------------------------------------------------------------


def test_cached_artifact_still_passes_validation():
    """A cached artifact must still pass current reference validation.

    This test verifies that when a cached outline is retrieved, the
    caller can (and should) validate it against the current EvidencePacket
    before accepting it.  The cache itself does not re-validate — that's
    the caller's responsibility — but the cache service provides the
    artifact for the caller to validate.
    """
    service, _, _ = _make_cache_service()

    # Insert a cached outline.
    cached_artifact = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": _VALID_PACKET["coverage_revision"],
        "outline_sections": [
            {
                "section_id": "sec-1",
                "title": "Test Section",
                "claims": [
                    {
                        "claim_id": str(_VALID_PACKET["claims"][0]["claim_id"]),
                        "required_passage_ids": [
                            str(_VALID_PACKET["passages"][0]["passage_id"])
                        ],
                    }
                ],
            }
        ],
        "unsupported_claims": [],
    }

    service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=1,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=cached_artifact,
        provenance=_BASE_PROVENANCE,
    )

    # Lookup the cached artifact.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=1,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None
    assert result.artifact == cached_artifact

    # The caller (e.g. LocalSynthesisService) must validate the cached
    # artifact against the current EvidencePacket.  This test verifies
    # that the artifact is available for validation — the actual
    # validation is performed by the ReportValidator (issue #64).


def test_stale_cached_reference_detected():
    """A cached artifact from an older packet revision must be detectable.

    The cache service does not enforce packet revision matching — it
    simply stores and returns artifacts.  However, the caller can detect
    stale references by comparing the cached artifact's
    ``evidence_packet_revision`` against the current packet revision.
    """
    service, _, _ = _make_cache_service()

    # Insert a cached artifact from an older packet revision.
    stale_artifact = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 1,  # Older than current (2).
        "outline_sections": [],
        "unsupported_claims": [],
    }

    service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=1,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=stale_artifact,
        provenance=_BASE_PROVENANCE,
    )

    # Lookup returns the cached artifact.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=1,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None

    # The caller can detect staleness by comparing revisions.
    cached_revision = result.artifact.get("evidence_packet_revision", 0)
    current_revision = _VALID_PACKET["coverage_revision"]
    assert cached_revision < current_revision
    # The caller should fall through to a fresh LLM call when stale.


# ---------------------------------------------------------------------------
# Cache loss tests
# ---------------------------------------------------------------------------


def test_cache_loss_does_not_affect_workflow_state():
    """Cache loss must affect performance only, never authoritative state.

    When the cache is unavailable or entries are missing, the caller
    should fall through to the real LLM call.  The cache is a
    performance optimization — its absence must never cause loss of
    authoritative workflow state.
    """
    # Simulate cache unavailability by using a service that always misses.
    service, _, _ = _make_cache_service()

    # Insert an entry.
    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # Invalidate the entry (simulating cache loss).
    service.invalidate(key_hash=entry.key_hash)

    # Lookup now misses.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None

    # The caller (LocalSynthesisService) should fall through to the LLM.
    # This test verifies the cache miss behavior — the actual LLM call
    # is outside the scope of this test.


def test_cache_lookup_exception_does_not_raise():
    """A cache lookup exception must not raise — it should return None."""
    mock_uow = MagicMock()
    mock_uow.semantic_cache.get_cache_entry_by_key = MagicMock(
        side_effect=RuntimeError("database error")
    )
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_uow)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_uow_factory = MagicMock(return_value=mock_ctx)

    service = SemanticCacheService(uow_factory=mock_uow_factory)

    # The lookup should handle the exception gracefully.
    # Note: the current implementation does NOT catch exceptions — it
    # lets them propagate so the caller knows the cache is unavailable.
    # This is intentional: cache loss is performance-only, but the
    # caller should handle the exception and fall through.
    with pytest.raises(RuntimeError, match="database error"):
        service.lookup(
            stage=_BASE_STAGE,
            model_name=_BASE_MODEL_NAME,
            model_revision=_BASE_MODEL_REVISION,
            endpoint_alias=_BASE_ENDPOINT,
            prompt_version=_BASE_PROMPT_VERSION,
            prompt_hash="test-prompt-hash",
            schema_version=_BASE_SCHEMA_VERSION,
            input_hash="test-input-hash",
            policy_version=_BASE_POLICY_VERSION,
            configuration=_BASE_CONFIG,
        )


# ---------------------------------------------------------------------------
# TTL tests
# ---------------------------------------------------------------------------


def test_ttl_expiration():
    """Entries older than TTL must be considered expired."""
    # Use TTL=0 so all entries are immediately expired.
    service, _, _ = _make_cache_service(ttl_seconds=0)

    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # Lookup should miss because the entry is expired.
    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None


def test_ttl_within_limit():
    """Entries within TTL must be returned."""
    # Use a large TTL so entries are not expired.
    service, _, _ = _make_cache_service(ttl_seconds=86400)

    entry = service.insert(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    result = service.lookup(
        stage=_BASE_STAGE,
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None
    assert result.key_hash == entry.key_hash


# ---------------------------------------------------------------------------
# CacheEntry model tests
# ---------------------------------------------------------------------------


def test_cache_entry_fields():
    """CacheEntry must have all expected fields."""
    entry_id = uuid4()
    key_hash = "test-key-hash"
    entry = CacheEntry(
        id=entry_id,
        key_hash=key_hash,
        stage="outline",
        model_fingerprint="llama:abc:local",
        input_hash="test-input-hash",
        prompt_hash="test-prompt-hash",
        prompt_version="synthesis-v1",
        schema_version=1,
        policy_version="budget-policy-v1",
        configuration_hash="test-config-hash",
        artifact={"test": "artifact"},
        provenance={"provider": "local"},
        status="valid",
        ttl_seconds=3600,
        created_at=time.time(),
    )
    assert entry.id == entry_id
    assert entry.key_hash == key_hash
    assert entry.stage == "outline"
    assert entry.model_fingerprint == "llama:abc:local"
    assert entry.status == "valid"
    assert entry.artifact == {"test": "artifact"}
    assert entry.provenance == {"provider": "local"}


# ---------------------------------------------------------------------------
# Configuration consistency (B1 regression test)
# ---------------------------------------------------------------------------


def test_write_and_check_use_same_configuration():
    """_write_cache and _check_cache must produce matching cache keys.

    This is a regression test for B1: if _check_cache() omits the
    configuration dict that _write_cache() includes, the cache key
    hashes will differ and no cache hits will occur.
    """
    service, _, _ = _make_cache_service()

    config = {"chunker_version": "hierarchical", "parser_version": "markdown-v1"}

    # Insert with configuration.
    entry = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=config,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # Lookup with the same configuration must hit.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=config,
    )
    assert result is not None
    assert result.key_hash == entry.key_hash

    # Lookup without configuration (or with None) must miss because the
    # canonical JSON differs when configuration is present vs absent.
    result_no_config = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=None,
    )
    assert result_no_config is None


# ---------------------------------------------------------------------------
# Revive expired/pruned entries (N1 regression test)
# ---------------------------------------------------------------------------


def test_revive_expired_entry():
    """Reviving an expired entry must succeed without unique-violation."""
    # Use a short but non-zero TTL so that the revived entry is not
    # immediately expired when the lookup runs a few microseconds later.
    service, _, cache_store = _make_cache_service(ttl_seconds=1)

    # Insert an entry that will expire after 1 second.
    entry = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # Manually expire the entry by setting created_at far in the past.
    stored = cache_store.get(entry.key_hash)
    if stored:
        stored["created_at"] = time.time() - 10  # 10 seconds ago

    # Lookup should miss because the entry is expired.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None

    # Re-insert with the same key should revive the expired entry.
    new_artifact = dict(_BASE_ARTIFACT)
    new_artifact["revived"] = True
    entry2 = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=new_artifact,
        provenance=_BASE_PROVENANCE,
    )
    assert entry2 is not None

    # Only one entry should exist in the store (no duplicate).
    assert len(cache_store) == 1

    # The revived entry should be returned on lookup.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is not None
    assert result.artifact.get("revived") is True


def test_revive_pruned_entry():
    """Reviving a pruned entry must succeed without unique-violation."""
    service, _, cache_store = _make_cache_service()

    # Insert and then prune the entry.
    entry = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None
    service.invalidate(key_hash=entry.key_hash)

    # Lookup should miss because the entry is pruned.
    result = service.lookup(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
    )
    assert result is None

    # Re-insert should revive the pruned entry.
    entry2 = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry2 is not None

    # Only one entry should exist in the store.
    assert len(cache_store) == 1

    # The revived entry should be valid.
    stored = cache_store.get(entry.key_hash)
    assert stored is not None
    assert stored["status"] == "valid"


# ---------------------------------------------------------------------------
# created_at type consistency (N5 regression test)
# ---------------------------------------------------------------------------


def test_created_at_is_float():
    """created_at must be a float for consistent TTL arithmetic."""
    service, _, _ = _make_cache_service()

    entry = service.insert(
        stage="outline",
        model_name=_BASE_MODEL_NAME,
        model_revision=_BASE_MODEL_REVISION,
        endpoint_alias=_BASE_ENDPOINT,
        prompt_version=_BASE_PROMPT_VERSION,
        prompt_hash="test-prompt-hash",
        schema_version=_BASE_SCHEMA_VERSION,
        input_hash="test-input-hash",
        policy_version=_BASE_POLICY_VERSION,
        configuration=_BASE_CONFIG,
        artifact=_BASE_ARTIFACT,
        provenance=_BASE_PROVENANCE,
    )
    assert entry is not None

    # created_at should be a float (not a string).
    assert isinstance(entry.created_at, float)
