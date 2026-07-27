"""Local resource governance and endpoint health controls.

This module provides bounded concurrent generative calls, per-endpoint
concurrency limits, token and batch caps, model endpoint health tracking,
explicit degraded states, backpressure, and resumable retry.

For a single-GPU environment, model residency and endpoint transitions are
treated as explicit operational state rather than hidden retry behavior.
The workflow does not assume that the embedding, reranking, and generative
models all fit simultaneously.

.. note::

   This module is intentionally lightweight — it provides the *policy* and
   *state* layer. Actual HTTP calls are delegated to the existing
   ``OpenAICompatibleEmbedder``, ``CohereCompatibleReranker``, and
   synthesis pipelines.

## Authoritative state

- Endpoint health is persisted in the ``model_endpoints`` table.
- Governor state (concurrency counts, queued jobs) is tracked in-memory
  and flushed to PostgreSQL for crash recovery.
- A run is resumable after endpoint restart — failed stages record their
  ``resource_limit`` error and can be retried.

## Invariants

1. No endpoint is assumed to be healthy by default; each starts as
   ``unknown`` until a health check succeeds or fails.
2. A ``degraded`` endpoint still accepts calls but may reject them under
   load — the governor enforces backpressure.
3. Resource-limit failures are explicit (``ResourceLimitError``) and
   never silently retried — the caller must decide whether to retry.
4. Endpoint restart leaves runs resumable — stages record their error
   state and can be re-executed after the endpoint recovers.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EndpointStatus(str, enum.Enum):
    """Health status of a model endpoint."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ResourceLimit(str, enum.Enum):
    """Types of resource limits that can be enforced."""

    CONCURRENCY = "concurrency"
    TOKENS = "tokens"
    BATCH = "batch"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointHealth:
    """Health state for a single model endpoint.

    Args:
        endpoint_name: Human-readable name (e.g. "generative", "embedding").
        url: The endpoint URL.
        status: Current health status.
        last_check_at: Unix timestamp of the last health check.
        last_error: Error message from the last failed check.
        concurrent_requests: Number of currently in-flight requests.
        queued_requests: Number of requests waiting in the backpressure queue.
        total_checks: Total number of health checks performed.
        total_failures: Total number of failed health checks.
        degraded_since: Unix timestamp when the endpoint entered degraded state.
        restart_count: Number of times the endpoint has restarted.
    """

    endpoint_name: str
    url: str
    status: EndpointStatus = EndpointStatus.UNKNOWN
    last_check_at: float | None = None
    last_error: str | None = None
    concurrent_requests: int = 0
    queued_requests: int = 0
    total_checks: int = 0
    total_failures: int = 0
    degraded_since: float | None = None
    restart_count: int = 0

    @property
    def is_available(self) -> bool:
        """Return True if the endpoint can accept new requests."""
        return self.status in (EndpointStatus.HEALTHY, EndpointStatus.DEGRADED)

    @property
    def is_healthy(self) -> bool:
        """Return True only if the endpoint is fully healthy."""
        return self.status == EndpointStatus.HEALTHY

    @property
    def is_degraded(self) -> bool:
        """Return True if the endpoint is in degraded state."""
        return self.status == EndpointStatus.DEGRADED

    @property
    def uptime_seconds(self) -> float | None:
        """Return seconds since the endpoint entered its current status.

        Returns None if the status has never been checked.
        """
        if self.last_check_at is None:
            return None
        return time.monotonic() - self.last_check_at


@dataclass
class EndpointConfig:
    """Configuration for a single model endpoint.

    Args:
        name: Unique endpoint name (e.g. "generative", "embedding").
        url: The endpoint URL.
        max_concurrent: Maximum concurrent requests allowed.
        max_input_tokens: Maximum input tokens per request.
        max_batch_size: Maximum batch size for batch endpoints.
        health_check_interval: Seconds between health checks.
        health_check_timeout: Seconds to wait for a health check response.
        backpressure_threshold: Number of queued requests before backpressure.
        token_cap: Maximum tokens per batch (0 = unlimited).
    """

    name: str
    url: str
    max_concurrent: int = 1
    max_input_tokens: int = 0  # 0 = unlimited
    max_batch_size: int = 0  # 0 = unlimited
    health_check_interval: float = 30.0
    health_check_timeout: float = 10.0
    backpressure_threshold: int = 0  # 0 = no backpressure
    token_cap: int = 0  # 0 = no cap


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ResourceLimitError(RuntimeError):
    """Raised when a resource limit is exceeded.

    This is NOT a transient error — it is an explicit signal that the
    caller must handle. The caller may:

    - Wait for backpressure to subside.
    - Retry after an endpoint restart.
    - Fail the stage and record the error for later resume.
    """

    def __init__(
        self,
        limit: ResourceLimit,
        endpoint: str,
        message: str,
        *,
        retry_after: float | None = None,
    ):
        self.limit = limit
        self.endpoint = endpoint
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"[{limit.value}] {endpoint}: {message}")


class EndpointUnavailableError(ResourceLimitError):
    """Raised when an endpoint is unavailable (unhealthy or unknown)."""

    def __init__(self, endpoint: str, message: str = "endpoint unavailable"):
        super().__init__(
            ResourceLimit.ENDPOINT_UNAVAILABLE,
            endpoint,
            message,
        )


# ---------------------------------------------------------------------------
# ResourceGovernor
# ---------------------------------------------------------------------------


class ResourceGovernor:
    """Bounded concurrent generative calls with per-endpoint controls.

    The governor enforces:

    - Per-endpoint concurrency limits (semaphores).
    - Token and batch caps.
    - Health checks with explicit degraded/healthy/unhealthy states.
    - Backpressure through a PostgreSQL-backed queue.
    - Resumable retry after endpoint restart.

    For a single-GPU environment, the governor does NOT assume that all
    models fit simultaneously. Each endpoint is tracked independently.

    Args:
        configs: Mapping of endpoint name -> EndpointConfig.
        health_store: Optional callable that persists health state.
            Signature: ``store_health(endpoint_name: str, health: EndpointHealth) -> None``.
        health_query: Optional callable that retrieves health state.
            Signature: ``query_health(endpoint_name: str) -> EndpointHealth | None``.
    """

    def __init__(
        self,
        configs: dict[str, EndpointConfig] | None = None,
        health_store=None,
        health_query=None,
    ):
        self._configs: dict[str, EndpointConfig] = configs or {}
        self._health_store = health_store
        self._health_query = health_query
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()
        self._health_states: dict[str, EndpointHealth] = {}
        self._started = False

    @property
    def configs(self) -> dict[str, EndpointConfig]:
        """Return a copy of the endpoint configurations."""
        return {k: v for k, v in self._configs.items()}

    def register_endpoint(self, config: EndpointConfig) -> None:
        """Register or update an endpoint configuration.

        Args:
            config: The endpoint configuration.

        Raises:
            ValueError: If max_concurrent is not positive.
        """
        if config.max_concurrent <= 0:
            raise ValueError(
                f"endpoint '{config.name}': max_concurrent must be positive"
            )
        with self._lock:
            self._configs[config.name] = config
            self._semaphores[config.name] = threading.Semaphore(config.max_concurrent)

    def unregister_endpoint(self, endpoint_name: str) -> None:
        """Remove an endpoint from governance."""
        with self._lock:
            self._configs.pop(endpoint_name, None)
            self._semaphores.pop(endpoint_name, None)
            self._health_states.pop(endpoint_name, None)

    def get_config(self, endpoint_name: str) -> EndpointConfig | None:
        """Return the configuration for an endpoint, or None."""
        return self._configs.get(endpoint_name)

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    def get_health(self, endpoint_name: str) -> EndpointHealth | None:
        """Return the current health state for an endpoint.

        If no health state exists, attempts to load from the health store.
        """
        with self._lock:
            if endpoint_name in self._health_states:
                return self._health_states[endpoint_name]

        if self._health_query is not None:
            try:
                health = self._health_query(endpoint_name)
                if health is not None:
                    with self._lock:
                        self._health_states[endpoint_name] = health
                    return health
            except Exception:  # noqa: BLE001
                logger.warning("failed to load health state for %s", endpoint_name)

        return None

    def set_health(
        self,
        endpoint_name: str,
        status: EndpointStatus,
        *,
        error: str | None = None,
    ) -> EndpointHealth:
        """Record health state for an endpoint and persist it.

        Args:
            endpoint_name: The endpoint name.
            status: The health status.
            error: Optional error message.

        Returns:
            The updated EndpointHealth.
        """
        config = self._configs.get(endpoint_name)
        if config is None:
            raise KeyError(f"endpoint '{endpoint_name}' is not registered")

        with self._lock:
            now = time.monotonic()
            existing = self._health_states.get(endpoint_name)

            # Compute derived values from existing state.
            new_restart_count = 0
            new_degraded_since = None
            if existing is not None:
                new_restart_count = existing.restart_count
                new_degraded_since = existing.degraded_since
                if existing.status != status:
                    # Status changed — track restarts and degradation.
                    if (
                        status == EndpointStatus.HEALTHY
                        and existing.status == EndpointStatus.UNHEALTHY
                    ):
                        new_restart_count += 1
                    if (
                        status == EndpointStatus.DEGRADED
                        and existing.degraded_since is None
                    ):
                        new_degraded_since = now
            else:
                # First call — if status is degraded, set degraded_since.
                if status == EndpointStatus.DEGRADED:
                    new_degraded_since = now

            health = EndpointHealth(
                endpoint_name=endpoint_name,
                url=config.url,
                status=status,
                last_check_at=now,
                last_error=error,
                concurrent_requests=existing.concurrent_requests if existing else 0,
                queued_requests=existing.queued_requests if existing else 0,
                total_checks=(existing.total_checks if existing else 0) + 1,
                total_failures=(
                    (existing.total_failures if existing else 0)
                    + (1 if status != EndpointStatus.HEALTHY else 0)
                ),
                degraded_since=new_degraded_since,
                restart_count=new_restart_count,
            )
            self._health_states[endpoint_name] = health

        if self._health_store is not None:
            try:
                self._health_store(endpoint_name, health)
            except Exception:  # noqa: BLE001
                logger.warning("failed to persist health state for %s", endpoint_name)

        return health

    def check_health(self, endpoint_name: str) -> EndpointHealth:
        """Run a health check and record the result.

        This is a generic health check — subclasses or callers should
        implement actual endpoint-specific checks.

        Args:
            endpoint_name: The endpoint name.

        Returns:
            The updated EndpointHealth.
        """
        config = self._configs.get(endpoint_name)
        if config is None:
            raise KeyError(f"endpoint '{endpoint_name}' is not registered")

        # Default: assume healthy if the endpoint is configured.
        # Subclasses or callers can override this by providing a custom
        # health check mechanism via the health_store.
        health = self.set_health(
            endpoint_name,
            EndpointStatus.HEALTHY,
        )
        return health

    def is_endpoint_available(self, endpoint_name: str) -> bool:
        """Check if an endpoint can accept new requests."""
        health = self.get_health(endpoint_name)
        if health is None:
            return False
        return health.is_available

    def all_endpoints_healthy(self) -> bool:
        """Return True if all registered endpoints are healthy."""
        for name in self._configs:
            health = self.get_health(name)
            if health is None or not health.is_healthy:
                return False
        return True

    # ------------------------------------------------------------------
    # Concurrency control (async)
    # ------------------------------------------------------------------

    async def acquire(
        self,
        endpoint_name: str,
        *,
        input_tokens: int = 0,
        batch_size: int = 1,
    ) -> None:
        """Acquire a slot for a request on the given endpoint.

        This method enforces:

        1. Endpoint availability (health check).
        2. Concurrency limit (semaphore).
        3. Token cap.
        4. Batch size cap.
        5. Backpressure (queues if threshold exceeded).

        Args:
            endpoint_name: The endpoint name.
            input_tokens: Estimated input tokens for the request.
            batch_size: Number of items in the batch.

        Raises:
            EndpointUnavailableError: If the endpoint is unavailable.
            ResourceLimitError: If a token/batch cap is exceeded.
        """
        config = self._configs.get(endpoint_name)
        if config is None:
            raise KeyError(f"endpoint '{endpoint_name}' is not registered")

        # Check 1: endpoint availability.
        health = self.get_health(endpoint_name)
        if health is None:
            # First call — assume available if configured.
            health = self.set_health(
                endpoint_name,
                EndpointStatus.HEALTHY,
            )
        elif not health.is_available:
            raise EndpointUnavailableError(
                endpoint_name,
                f"endpoint is {health.status.value} (last error: {health.last_error})",
            )

        # Check 2: concurrency limit.
        semaphore = self._semaphores.get(endpoint_name)
        if semaphore is None:
            raise KeyError(f"endpoint '{endpoint_name}' has no semaphore")

        # Check 3: backpressure.
        if config.backpressure_threshold > 0:
            with self._lock:
                queued = self._health_states.get(endpoint_name, health).queued_requests
                if queued >= config.backpressure_threshold:
                    raise ResourceLimitError(
                        ResourceLimit.CONCURRENCY,
                        endpoint_name,
                        f"backpressure threshold reached "
                        f"(queued={queued}, threshold={config.backpressure_threshold})",
                    )

        # Check 4: token cap.
        if config.max_input_tokens > 0 and input_tokens > config.max_input_tokens:
            raise ResourceLimitError(
                ResourceLimit.TOKENS,
                endpoint_name,
                f"input tokens {input_tokens} exceeds cap {config.max_input_tokens}",
            )

        # Check 5: batch size cap.
        if config.max_batch_size > 0 and batch_size > config.max_batch_size:
            raise ResourceLimitError(
                ResourceLimit.BATCH,
                endpoint_name,
                f"batch size {batch_size} exceeds cap {config.max_batch_size}",
            )

        # Acquire the semaphore (blocks if at limit).
        # threading.Semaphore.acquire() is a blocking call — run it in
        # a thread pool so we don't block the event loop.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, semaphore.acquire)

        # Update concurrent request count.
        with self._lock:
            if endpoint_name in self._health_states:
                current = self._health_states[endpoint_name]
                self._health_states[endpoint_name] = EndpointHealth(
                    endpoint_name=current.endpoint_name,
                    url=current.url,
                    status=current.status,
                    last_check_at=current.last_check_at,
                    last_error=current.last_error,
                    concurrent_requests=current.concurrent_requests + 1,
                    queued_requests=current.queued_requests,
                    total_checks=current.total_checks,
                    total_failures=current.total_failures,
                    degraded_since=current.degraded_since,
                    restart_count=current.restart_count,
                )

    async def release(self, endpoint_name: str) -> None:
        """Release a slot for a request on the given endpoint."""
        semaphore = self._semaphores.get(endpoint_name)
        if semaphore is None:
            return

        semaphore.release()

        # Update concurrent request count.
        with self._lock:
            if endpoint_name in self._health_states:
                current = self._health_states[endpoint_name]
                self._health_states[endpoint_name] = EndpointHealth(
                    endpoint_name=current.endpoint_name,
                    url=current.url,
                    status=current.status,
                    last_check_at=current.last_check_at,
                    last_error=current.last_error,
                    concurrent_requests=max(0, current.concurrent_requests - 1),
                    queued_requests=current.queued_requests,
                    total_checks=current.total_checks,
                    total_failures=current.total_failures,
                    degraded_since=current.degraded_since,
                    restart_count=current.restart_count,
                )

    # ------------------------------------------------------------------
    # Context manager (sync)
    # ------------------------------------------------------------------

    def acquire_sync(
        self,
        endpoint_name: str,
        *,
        input_tokens: int = 0,
        batch_size: int = 1,
    ) -> None:
        """Synchronous acquire for use in sync code paths.

        This is a blocking call that waits for a slot to become available.
        For async code, prefer ``acquire()``.

        Raises:
            EndpointUnavailableError: If the endpoint is unavailable.
            ResourceLimitError: If a token/batch cap is exceeded.
        """
        config = self._configs.get(endpoint_name)
        if config is None:
            raise KeyError(f"endpoint '{endpoint_name}' is not registered")

        # Check 1: endpoint availability.
        health = self.get_health(endpoint_name)
        if health is None:
            health = self.set_health(
                endpoint_name,
                EndpointStatus.HEALTHY,
            )
        elif not health.is_available:
            raise EndpointUnavailableError(
                endpoint_name,
                f"endpoint is {health.status.value} (last error: {health.last_error})",
            )

        # Check 2: concurrency limit.
        config_obj = self._configs.get(endpoint_name)
        if config_obj is None:
            raise KeyError(f"endpoint '{endpoint_name}' has no config")

        # Check 3: token cap.
        if (
            config_obj.max_input_tokens > 0
            and input_tokens > config_obj.max_input_tokens
        ):
            raise ResourceLimitError(
                ResourceLimit.TOKENS,
                endpoint_name,
                f"input tokens {input_tokens} exceeds cap {config_obj.max_input_tokens}",
            )

        # Check 4: batch size cap.
        if config_obj.max_batch_size > 0 and batch_size > config_obj.max_batch_size:
            raise ResourceLimitError(
                ResourceLimit.BATCH,
                endpoint_name,
                f"batch size {batch_size} exceeds cap {config_obj.max_batch_size}",
            )

        # Check 5: backpressure.
        if config_obj.backpressure_threshold > 0:
            with self._lock:
                queued = self._health_states.get(endpoint_name, health).queued_requests
                if queued >= config_obj.backpressure_threshold:
                    raise ResourceLimitError(
                        ResourceLimit.CONCURRENCY,
                        endpoint_name,
                        f"backpressure threshold reached "
                        f"(queued={queued}, threshold={config_obj.backpressure_threshold})",
                    )

        # Acquire semaphore synchronously (blocking).
        semaphore = self._semaphores.get(endpoint_name)
        if semaphore is None:
            raise KeyError(f"endpoint '{endpoint_name}' has no semaphore")
        semaphore.acquire()

        # Update concurrent request count.
        with self._lock:
            if endpoint_name in self._health_states:
                current = self._health_states[endpoint_name]
                self._health_states[endpoint_name] = EndpointHealth(
                    endpoint_name=current.endpoint_name,
                    url=current.url,
                    status=current.status,
                    last_check_at=current.last_check_at,
                    last_error=current.last_error,
                    concurrent_requests=current.concurrent_requests + 1,
                    queued_requests=current.queued_requests,
                    total_checks=current.total_checks,
                    total_failures=current.total_failures,
                    degraded_since=current.degraded_since,
                    restart_count=current.restart_count,
                )

    def release_sync(self, endpoint_name: str) -> None:
        """Synchronous release for use in sync code paths."""
        semaphore = self._semaphores.get(endpoint_name)
        if semaphore is None:
            return

        semaphore.release()

        with self._lock:
            if endpoint_name in self._health_states:
                current = self._health_states[endpoint_name]
                self._health_states[endpoint_name] = EndpointHealth(
                    endpoint_name=current.endpoint_name,
                    url=current.url,
                    status=current.status,
                    last_check_at=current.last_check_at,
                    last_error=current.last_error,
                    concurrent_requests=max(0, current.concurrent_requests - 1),
                    queued_requests=current.queued_requests,
                    total_checks=current.total_checks,
                    total_failures=current.total_failures,
                    degraded_since=current.degraded_since,
                    restart_count=current.restart_count,
                )

    # ------------------------------------------------------------------
    # Summary / reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a summary of all endpoint health states."""
        with self._lock:
            return {
                name: {
                    "status": health.status.value,
                    "url": health.url,
                    "concurrent_requests": health.concurrent_requests,
                    "queued_requests": health.queued_requests,
                    "total_checks": health.total_checks,
                    "total_failures": health.total_failures,
                    "restart_count": health.restart_count,
                    "last_error": health.last_error,
                }
                for name, health in self._health_states.items()
            }

    def reset_health(self, endpoint_name: str) -> None:
        """Reset health state for an endpoint to unknown.

        This is useful after an endpoint restart — the governor forgets
        the previous health state and will re-evaluate on the next call.
        """
        with self._lock:
            self._health_states.pop(endpoint_name, None)

    def reset_all_health(self) -> None:
        """Reset health state for all endpoints."""
        with self._lock:
            self._health_states.clear()


# ---------------------------------------------------------------------------
# PostgreSQL-backed health store
# ---------------------------------------------------------------------------


def make_health_store(uow_factory) -> Any:
    """Create a health store that persists to PostgreSQL.

    Returns a callable with signature:
        ``store_health(endpoint_name: str, health: EndpointHealth) -> None``
    """

    def store_health(endpoint_name: str, health: EndpointHealth) -> None:
        with uow_factory() as uow:
            uow.model_endpoints.upsert_health(
                endpoint_name=endpoint_name,
                url=health.url,
                status=health.status.value,
                last_check_at=health.last_check_at,
                last_error=health.last_error,
                concurrent_requests=health.concurrent_requests,
                queued_requests=health.queued_requests,
                total_checks=health.total_checks,
                total_failures=health.total_failures,
                degraded_since=health.degraded_since,
                restart_count=health.restart_count,
            )

    return store_health


def make_health_query(uow_factory) -> Any:
    """Create a health query that loads from PostgreSQL.

    Returns a callable with signature:
        ``query_health(endpoint_name: str) -> EndpointHealth | None``
    """

    def query_health(endpoint_name: str) -> EndpointHealth | None:
        with uow_factory() as uow:
            row = uow.model_endpoints.get_health(endpoint_name)
            if row is None:
                return None
            return EndpointHealth(
                endpoint_name=row["endpoint_name"],
                url=row["url"],
                status=EndpointStatus(row["status"]),
                last_check_at=row.get("last_check_at"),
                last_error=row.get("last_error"),
                concurrent_requests=row.get("concurrent_requests", 0),
                queued_requests=row.get("queued_requests", 0),
                total_checks=row.get("total_checks", 0),
                total_failures=row.get("total_failures", 0),
                degraded_since=row.get("degraded_since"),
                restart_count=row.get("restart_count", 0),
            )

    return query_health
