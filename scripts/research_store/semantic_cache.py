"""Semantic-result cache for bounded synthesis stages.

This module provides a PostgreSQL-backed cache for semantic-stage outputs.
A cache entry is identified by a deterministic key derived from:

* semantic stage (``outline``, ``binding``, ``draft``, ``citation_pass``),
* prompt version and hash,
* output schema version,
* model fingerprint (model name + revision + endpoint alias),
* input hash (hash of the structured input payload),
* policy version,
* relevant configuration (chunker version, parser version).

A cached output **still passes current reference and state validation**
before the caller accepts it.  Cache loss affects performance only — it
never causes loss of authoritative workflow state.

Valkey may accelerate lookups but must never be the sole durable cache
record when provenance is required.

## Idempotency

Cache insertion is idempotent: if a valid, non-expired entry already exists
for the computed key, the insert is a no-op.

## Architecture

* PostgreSQL is the authoritative store for cache identity and provenance.
* The cache service is stateless and uses the UOW factory for all DB access.
* Cache lookups are read-only; inserts are write-only.
* Cache pruning removes expired or explicitly removed entries.
* The cache is transparent to the synthesis pipeline — if a cache hit
  produces a result that fails validation, the caller falls through to
  the real LLM call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------

# Default TTL for cache entries (seconds).  Entries older than this are
# considered expired and will not be returned on lookup.
_DEFAULT_TTL_SECONDS = 3600  # 1 hour


def _compute_cache_key(
    *,
    stage: str,
    prompt_version: str,
    prompt_hash: str,
    schema_version: int,
    model_name: str,
    model_revision: str,
    endpoint_alias: str,
    input_hash: str,
    policy_version: str | None,
    configuration: dict[str, str] | None,
) -> str:
    """Compute a SHA-256 cache key from the applicable identity components.

    Args:
        stage: The synthesis stage name.
        prompt_version: The prompt template version.
        prompt_hash: SHA-256 hash of the system+user prompt content.
        schema_version: The output schema version.
        model_name: The model name used for the call.
        model_revision: The model revision (e.g. git SHA, tag).
        endpoint_alias: The endpoint alias (e.g. "local", "openai").
        input_hash: SHA-256 hash of the structured input payload.
        policy_version: The budget policy version (may be None).
        configuration: Additional configuration components (chunker version,
            parser version, etc.).

    Returns:
        A hex SHA-256 digest string.
    """
    fingerprint = f"{model_name}:{model_revision}:{endpoint_alias}"
    components = {
        "stage": stage,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "schema_version": schema_version,
        "model_fingerprint": fingerprint,
        "input_hash": input_hash,
        "policy_version": policy_version or "",
    }
    if configuration:
        components["configuration"] = dict(configuration)

    canonical = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache entry model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """A semantic-result cache entry.

    Attributes:
        id: The cache row UUID.
        key_hash: The SHA-256 cache key (hex digest).
        stage: The synthesis stage name.
        model_fingerprint: The model fingerprint used.
        input_hash: Hash of the structured input.
        prompt_hash: Hash of the prompt content.
        prompt_version: The prompt template version.
        schema_version: The output schema version.
        policy_version: The policy version.
        configuration_hash: Hash of the configuration dict.
        artifact: The cached output artifact.
        provenance: Metadata about the original call.
        status: "valid", "expired", or "pruned".
        ttl_seconds: Time-to-live in seconds.
        created_at: When the entry was created.
    """

    id: UUID
    key_hash: str
    stage: str
    model_fingerprint: str
    input_hash: str
    prompt_hash: str
    prompt_version: str
    schema_version: int
    policy_version: str | None
    configuration_hash: str | None
    artifact: dict[str, Any]
    provenance: dict[str, Any]
    status: str
    ttl_seconds: int
    created_at: str


# ---------------------------------------------------------------------------
# Cache service
# ---------------------------------------------------------------------------


class SemanticCacheService:
    """PostgreSQL-backed semantic-result cache.

    Args:
        uow_factory: A callable that returns a UOW context manager.
        ttl_seconds: Time-to-live for cache entries in seconds.
    """

    def __init__(
        self,
        uow_factory: Any,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self.uow_factory = uow_factory
        self.ttl_seconds = ttl_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(
        self,
        *,
        stage: str,
        model_name: str,
        model_revision: str,
        endpoint_alias: str,
        prompt_version: str,
        prompt_hash: str,
        schema_version: int,
        input_hash: str,
        policy_version: str | None = None,
        configuration: dict[str, str] | None = None,
    ) -> CacheEntry | None:
        """Look up a valid cache entry by its computed key.

        Returns the cached artifact if a valid, non-expired entry exists.
        Returns ``None`` if no entry matches or the entry is expired.

        The lookup is read-only and does not mutate any state.
        """
        key_hash = _compute_cache_key(
            stage=stage,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=schema_version,
            model_name=model_name,
            model_revision=model_revision,
            endpoint_alias=endpoint_alias,
            input_hash=input_hash,
            policy_version=policy_version,
            configuration=configuration,
        )

        with self.uow_factory() as uow:
            row = uow.semantic_cache.get_cache_entry_by_key(key_hash)
            if row is None:
                return None

            entry = self._row_to_entry(row)
            if entry.status != "valid":
                return None

            # Check TTL expiration.
            if self._is_expired(entry):
                self._mark_expired(uow, entry.id)
                return None

            return entry

    def insert(
        self,
        *,
        stage: str,
        model_name: str,
        model_revision: str,
        endpoint_alias: str,
        prompt_version: str,
        prompt_hash: str,
        schema_version: int,
        input_hash: str,
        policy_version: str | None,
        configuration: dict[str, str] | None,
        artifact: dict[str, Any],
        provenance: dict[str, Any],
    ) -> CacheEntry | None:
        """Insert a cache entry.

        Idempotent: if a valid entry already exists for the key, returns
        the existing entry without inserting a duplicate.

        Args:
            stage: The synthesis stage name.
            model_name: The model name used.
            model_revision: The model revision.
            endpoint_alias: The endpoint alias.
            prompt_version: The prompt template version.
            prompt_hash: Hash of the prompt content.
            schema_version: The output schema version.
            input_hash: Hash of the structured input.
            policy_version: The policy version.
            configuration: Additional configuration.
            artifact: The cached output artifact.
            provenance: Metadata about the original call.

        Returns:
            The inserted (or existing) cache entry, or ``None`` if the
            artifact is empty or invalid.
        """
        key_hash = _compute_cache_key(
            stage=stage,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=schema_version,
            model_name=model_name,
            model_revision=model_revision,
            endpoint_alias=endpoint_alias,
            input_hash=input_hash,
            policy_version=policy_version,
            configuration=configuration,
        )

        if not artifact:
            logger.debug("semantic cache: empty artifact, skipping insert")
            return None

        config_hash = None
        if configuration:
            config_hash = hashlib.sha256(
                json.dumps(
                    configuration, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()

        with self.uow_factory() as uow:
            # Check for existing valid entry (idempotent insert).
            existing = uow.semantic_cache.get_cache_entry_by_key(key_hash)
            if existing is not None:
                entry = self._row_to_entry(existing)
                if entry.status == "valid" and not self._is_expired(entry):
                    logger.debug(
                        "semantic cache: idempotent insert — existing valid entry for key %s",
                        key_hash[:12],
                    )
                    return entry

            # Insert new entry.
            entry_id = uuid4()
            now = time.time()
            record = {
                "id": entry_id,
                "key_hash": key_hash,
                "stage": stage,
                "model_fingerprint": f"{model_name}:{model_revision}:{endpoint_alias}",
                "input_hash": input_hash,
                "prompt_hash": prompt_hash,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "policy_version": policy_version,
                "configuration_hash": config_hash,
                "artifact": artifact,
                "provenance": provenance,
                "status": "valid",
                "ttl_seconds": self.ttl_seconds,
                "created_at": now,
            }
            uow.semantic_cache.insert_cache_entry(record)
            logger.debug(
                "semantic cache: inserted entry %s for stage %s (key %s...)",
                entry_id,
                stage,
                key_hash[:12],
            )
            return self._row_to_entry(record)

    def prune(self, *, older_than_seconds: int | None = None) -> int:
        """Prune expired or stale cache entries.

        Args:
            older_than_seconds: If provided, only prune entries older than
                this many seconds.  If ``None``, prunes all expired entries.

        Returns:
            The number of entries pruned.
        """
        with self.uow_factory() as uow:
            count = uow.semantic_cache.prune_cache_entries(
                older_than_seconds=older_than_seconds,
                ttl_seconds=self.ttl_seconds,
            )
            if count:
                logger.info("semantic cache: pruned %d entries", count)
            return count

    def invalidate(self, *, key_hash: str) -> bool:
        """Explicitly invalidate a cache entry by key.

        Args:
            key_hash: The cache key hash to invalidate.

        Returns:
            ``True`` if an entry was invalidated, ``False`` if none matched.
        """
        with self.uow_factory() as uow:
            count = uow.semantic_cache.invalidate_cache_entry(key_hash)
            if count:
                logger.info("semantic cache: invalidated entry %s", key_hash[:12])
            return count > 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> CacheEntry:
        """Convert a database row dict to a CacheEntry."""

        def _to_uuid(value):
            if value is None:
                return None
            if isinstance(value, UUID):
                return value
            return UUID(value)

        created_at = row["created_at"]
        if isinstance(created_at, (int, float)):
            created_at = str(created_at)

        return CacheEntry(
            id=_to_uuid(row["id"]),
            key_hash=row["key_hash"],
            stage=row["stage"],
            model_fingerprint=row["model_fingerprint"],
            input_hash=row["input_hash"],
            prompt_hash=row["prompt_hash"],
            prompt_version=row["prompt_version"],
            schema_version=int(row["schema_version"]),
            policy_version=row.get("policy_version"),
            configuration_hash=row.get("configuration_hash"),
            artifact=row.get("artifact") or {},
            provenance=row.get("provenance") or {},
            status=row["status"],
            ttl_seconds=int(row["ttl_seconds"]),
            created_at=created_at,
        )

    def _is_expired(self, entry: CacheEntry) -> bool:
        """Check if a cache entry has exceeded its TTL."""
        try:
            created = float(entry.created_at)
        except (ValueError, TypeError):
            return True
        age = time.time() - created
        return age > entry.ttl_seconds

    @staticmethod
    def _mark_expired(uow: Any, entry_id: UUID) -> None:
        """Mark a cache entry as expired."""
        try:
            uow.semantic_cache.invalidate_cache_entry_by_id(entry_id)
        except Exception:  # noqa: BLE001
            # Best-effort — cache state loss is non-authoritative.
            logger.debug("semantic cache: failed to mark entry %s as expired", entry_id)

    def compute_input_hash(self, input_payload: dict[str, Any]) -> str:
        """Compute a deterministic hash of an input payload.

        Args:
            input_payload: The structured input dict (claims, passages,
                bindings, etc.).

        Returns:
            A hex SHA-256 digest.
        """
        canonical = json.dumps(
            input_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def compute_prompt_hash(self, system_prompt: str, user_prompt: str) -> str:
        """Compute a deterministic hash of prompt content.

        Args:
            system_prompt: The system prompt string.
            user_prompt: The user prompt string.

        Returns:
            A hex SHA-256 digest.
        """
        combined = (system_prompt + "\n" + user_prompt).encode("utf-8")
        return hashlib.sha256(combined).hexdigest()


__all__ = [
    "CacheEntry",
    "SemanticCacheService",
    "_compute_cache_key",
]
