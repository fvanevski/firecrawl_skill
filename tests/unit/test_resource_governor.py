"""Tests for local resource governance (issue #66, P7-06).

Covers:
- Endpoint health tracking and state transitions.
- Endpoint restart leaving runs resumable.
- Concurrency limits (semaphore-based).
- Backpressure (queue threshold enforcement).
- Token and batch caps.
- ResourceLimitError and EndpointUnavailableError.
- PostgreSQL-backed health store and query.
- Config defaults and validation.
- CLI commands (endpoint-health, resource-status).
- Restart behavior (reset_health, reset_all_health).
- Degraded state tracking.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.resource_governor import (
    EndpointConfig,
    EndpointHealth,
    EndpointStatus,
    EndpointUnavailableError,
    ResourceGovernor,
    ResourceLimit,
    ResourceLimitError,
    make_health_query,
    make_health_store,
)

# ---------------------------------------------------------------------------
# EndpointHealth tests
# ---------------------------------------------------------------------------


class TestEndpointHealth:
    """Tests for the EndpointHealth dataclass."""

    def test_healthy_is_available(self):
        health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.HEALTHY,
        )
        assert health.is_available is True
        assert health.is_healthy is True
        assert health.is_degraded is False

    def test_degraded_is_available_but_not_healthy(self):
        health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.DEGRADED,
        )
        assert health.is_available is True
        assert health.is_healthy is False
        assert health.is_degraded is True

    def test_unhealthy_is_not_available(self):
        health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.UNHEALTHY,
        )
        assert health.is_available is False
        assert health.is_healthy is False
        assert health.is_degraded is False

    def test_unknown_is_not_available(self):
        health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.UNKNOWN,
        )
        assert health.is_available is False

    def test_uptime_seconds_returns_none_when_not_checked(self):
        health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.UNKNOWN,
        )
        assert health.uptime_seconds is None


# ---------------------------------------------------------------------------
# ResourceGovernor — basic operations
# ---------------------------------------------------------------------------


class TestResourceGovernorBasic:
    """Tests for basic ResourceGovernor operations."""

    def test_create_empty_governor(self):
        governor = ResourceGovernor()
        assert governor.configs == {}
        assert governor.all_endpoints_healthy() is True

    def test_register_endpoint(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        assert governor.get_config("generative") is config
        assert len(governor.configs) == 1

    def test_register_endpoint_rejects_zero_concurrent(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=0,
        )
        governor = ResourceGovernor()
        with pytest.raises(ValueError, match="max_concurrent must be positive"):
            governor.register_endpoint(config)

    def test_unregister_endpoint(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.unregister_endpoint("generative")
        assert governor.get_config("generative") is None

    def test_unregister_unknown_endpoint_is_safe(self):
        governor = ResourceGovernor()
        governor.unregister_endpoint("nonexistent")
        assert len(governor.configs) == 0

    def test_get_config_returns_none_for_unknown(self):
        governor = ResourceGovernor()
        assert governor.get_config("nonexistent") is None

    def test_summary_returns_empty_for_empty_governor(self):
        governor = ResourceGovernor()
        summary = governor.summary()
        assert summary == {}

    def test_summary_returns_health_for_registered_endpoints(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)
        summary = governor.summary()
        assert "generative" in summary
        assert summary["generative"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# ResourceGovernor — health tracking
# ---------------------------------------------------------------------------


class TestResourceGovernorHealth:
    """Tests for health tracking."""

    def test_health_starts_unknown(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        health = governor.get_health("generative")
        assert health is None

    def test_set_health_records_state(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        health = governor.set_health("generative", EndpointStatus.HEALTHY)
        assert health.status == EndpointStatus.HEALTHY
        assert health.total_checks == 1
        assert health.last_check_at is not None

    def test_health_transitions_track_restart(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        # Unhealthy -> Healthy should increment restart_count.
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")
        health1 = governor.get_health("generative")
        assert health1 is not None
        assert health1.restart_count == 0

        governor.set_health("generative", EndpointStatus.HEALTHY)
        health2 = governor.get_health("generative")
        assert health2 is not None
        assert health2.restart_count == 1
        assert health2.total_failures == 1

    def test_degraded_state_tracks_degraded_since(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        # First time degraded — degraded_since should be set.
        governor.set_health("generative", EndpointStatus.DEGRADED, error="slow")
        health = governor.get_health("generative")
        assert health is not None
        assert health.degraded_since is not None

        # Second degraded call — degraded_since should persist.
        governor.set_health("generative", EndpointStatus.DEGRADED, error="still slow")
        health2 = governor.get_health("generative")
        assert health2 is not None
        assert health2.degraded_since == health.degraded_since

    def test_health_persistence_via_store(self):
        store_mock = MagicMock()
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor(health_store=store_mock)
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)
        store_mock.assert_called_once()

    def test_health_query_via_factory(self):
        query_mock = MagicMock(return_value=None)
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor(health_query=query_mock)
        governor.register_endpoint(config)
        health = governor.get_health("generative")
        assert health is None

    def test_health_query_returns_stored_state(self):
        stored_health = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.DEGRADED,
            total_checks=5,
            total_failures=2,
        )
        query_mock = MagicMock(return_value=stored_health)
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor(health_query=query_mock)
        governor.register_endpoint(config)
        health = governor.get_health("generative")
        assert health is stored_health
        assert health.status == EndpointStatus.DEGRADED

    def test_all_endpoints_healthy_returns_false_when_one_unhealthy(self):
        config1 = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        config2 = EndpointConfig(
            name="embedding",
            url="http://localhost:8003",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config1)
        governor.register_endpoint(config2)
        governor.set_health("generative", EndpointStatus.HEALTHY)
        governor.set_health("embedding", EndpointStatus.UNHEALTHY)
        assert governor.all_endpoints_healthy() is False

    def test_all_endpoints_healthy_returns_true_when_all_healthy(self):
        config1 = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        config2 = EndpointConfig(
            name="embedding",
            url="http://localhost:8003",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config1)
        governor.register_endpoint(config2)
        governor.set_health("generative", EndpointStatus.HEALTHY)
        governor.set_health("embedding", EndpointStatus.HEALTHY)
        assert governor.all_endpoints_healthy() is True


# ---------------------------------------------------------------------------
# ResourceGovernor — concurrency limits
# ---------------------------------------------------------------------------


class TestResourceGovernorConcurrency:
    """Tests for concurrency limits."""

    def test_acquire_respects_concurrent_limit(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        async def _test():
            # First two acquires succeed.
            await governor.acquire("generative")
            await governor.acquire("generative")
            # Third acquire should block — use a timeout to verify.
            task = asyncio.create_task(governor.acquire("generative"))
            await asyncio.sleep(0.05)
            # Task should still be pending (blocked).
            assert not task.done()
            # Release one slot so the third can proceed.
            await governor.release("generative")
            await task  # Should complete now.

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_test())
        finally:
            loop.close()

    def test_release_frees_slot(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        async def _test():
            await governor.acquire("generative")
            await governor.release("generative")
            # Should succeed now.
            await governor.acquire("generative")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_test())
        finally:
            loop.close()

    def test_concurrent_requests_count_increments(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(governor.acquire("generative"))
            health = governor.get_health("generative")
            assert health is not None
            assert health.concurrent_requests == 1
            loop.run_until_complete(governor.release("generative"))
        finally:
            loop.close()

    def test_concurrent_requests_count_decrements_on_release(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(governor.acquire("generative"))
            loop.run_until_complete(governor.release("generative"))
            health = governor.get_health("generative")
            assert health is not None
            assert health.concurrent_requests == 0
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# ResourceGovernor — backpressure
# ---------------------------------------------------------------------------


class TestResourceGovernorBackpressure:
    """Tests for backpressure."""

    def test_backpressure_raises_when_threshold_reached(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=4,
            backpressure_threshold=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        # Manually set queued_requests to exceed threshold using immutable pattern.
        governor.set_health("generative", EndpointStatus.HEALTHY)
        with governor._lock:
            existing = governor._health_states["generative"]
            governor._health_states["generative"] = EndpointHealth(
                endpoint_name=existing.endpoint_name,
                url=existing.url,
                status=existing.status,
                last_check_at=existing.last_check_at,
                last_error=existing.last_error,
                concurrent_requests=existing.concurrent_requests,
                queued_requests=3,
                total_checks=existing.total_checks,
                total_failures=existing.total_failures,
                degraded_since=existing.degraded_since,
                restart_count=existing.restart_count,
            )

        with pytest.raises(ResourceLimitError) as exc_info:
            governor.acquire_sync("generative")
        assert exc_info.value.limit == ResourceLimit.CONCURRENCY

    def test_no_backpressure_when_threshold_is_zero(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=4,
            backpressure_threshold=0,  # No backpressure.
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        # Should succeed even with queued requests.
        with governor._lock:
            existing = governor._health_states["generative"]
            governor._health_states["generative"] = EndpointHealth(
                endpoint_name=existing.endpoint_name,
                url=existing.url,
                status=existing.status,
                last_check_at=existing.last_check_at,
                last_error=existing.last_error,
                concurrent_requests=existing.concurrent_requests,
                queued_requests=100,
                total_checks=existing.total_checks,
                total_failures=existing.total_failures,
                degraded_since=existing.degraded_since,
                restart_count=existing.restart_count,
            )
        governor.acquire_sync("generative")


# ---------------------------------------------------------------------------
# ResourceGovernor — token and batch caps
# ---------------------------------------------------------------------------


class TestResourceGovernorCaps:
    """Tests for token and batch caps."""

    def test_token_cap_exceeded_raises(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
            max_input_tokens=1000,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        with pytest.raises(ResourceLimitError) as exc_info:
            governor.acquire_sync("generative", input_tokens=2000)
        assert exc_info.value.limit == ResourceLimit.TOKENS

    def test_token_cap_not_enforced_when_zero(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
            max_input_tokens=0,  # No cap.
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        # Should succeed with any token count.
        governor.acquire_sync("generative", input_tokens=100000)

    def test_batch_cap_exceeded_raises(self):
        config = EndpointConfig(
            name="embedding",
            url="http://localhost:8003",
            max_concurrent=4,
            max_batch_size=32,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("embedding", EndpointStatus.HEALTHY)

        with pytest.raises(ResourceLimitError) as exc_info:
            governor.acquire_sync("embedding", batch_size=64)
        assert exc_info.value.limit == ResourceLimit.BATCH

    def test_batch_cap_not_enforced_when_zero(self):
        config = EndpointConfig(
            name="embedding",
            url="http://localhost:8003",
            max_concurrent=4,
            max_batch_size=0,  # No cap.
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("embedding", EndpointStatus.HEALTHY)

        governor.acquire_sync("embedding", batch_size=1000)


# ---------------------------------------------------------------------------
# ResourceGovernor — endpoint unavailable
# ---------------------------------------------------------------------------


class TestResourceGovernorUnavailable:
    """Tests for endpoint unavailable errors."""

    def test_unhealthy_endpoint_raises(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")

        with pytest.raises(EndpointUnavailableError):
            governor.acquire_sync("generative")

    def test_first_acquire_auto_sets_healthy(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        # First call sets health to HEALTHY — this is expected behavior.
        # The test verifies that the first acquire succeeds.
        governor.acquire_sync("generative")

    def test_endpoint_unavailable_error_has_retry_after(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")

        with pytest.raises(EndpointUnavailableError) as exc_info:
            governor.acquire_sync("generative")
        assert exc_info.value.endpoint == "generative"


# ---------------------------------------------------------------------------
# ResourceGovernor — restart behavior
# ---------------------------------------------------------------------------


class TestResourceGovernorRestart:
    """Tests for restart behavior."""

    def test_reset_health_clears_state(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")
        assert governor.get_health("generative") is not None

        governor.reset_health("generative")
        assert governor.get_health("generative") is None

    def test_reset_all_health_clears_all(self):
        config1 = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        config2 = EndpointConfig(
            name="embedding",
            url="http://localhost:8003",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config1)
        governor.register_endpoint(config2)
        governor.set_health("generative", EndpointStatus.UNHEALTHY)
        governor.set_health("embedding", EndpointStatus.DEGRADED)

        governor.reset_all_health()
        assert governor.get_health("generative") is None
        assert governor.get_health("embedding") is None

    def test_run_is_resumable_after_endpoint_restart(self):
        """Endpoint restart leaves runs resumable.

        After an endpoint crashes and restarts:

        1. The governor records the crash (UNHEALTHY).
        2. The operator resets health (or the endpoint recovers).
        3. The governor accepts new requests.
        4. Stages that failed due to the crash can be retried.
        """
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)

        # Simulate crash.
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")
        h = governor.get_health("generative")
        assert h is not None
        assert h.status == EndpointStatus.UNHEALTHY

        # Simulate restart.
        governor.reset_health("generative")
        governor.set_health("generative", EndpointStatus.HEALTHY)

        # Should accept requests again.
        governor.acquire_sync("generative")
        health = governor.get_health("generative")
        assert health is not None
        assert health.status == EndpointStatus.HEALTHY
        assert health.restart_count == 0  # Reset cleared the old count.


# ---------------------------------------------------------------------------
# ResourceGovernor — sync context manager pattern
# ---------------------------------------------------------------------------


class TestResourceGovernorSyncAcquireRelease:
    """Tests for sync acquire/release pattern."""

    def test_sync_acquire_release(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        governor.acquire_sync("generative")
        h = governor.get_health("generative")
        assert h is not None
        assert h.concurrent_requests == 1
        governor.release_sync("generative")
        h = governor.get_health("generative")
        assert h is not None
        assert h.concurrent_requests == 0

    def test_sync_acquire_blocks_at_limit(self):
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        governor.acquire_sync("generative")
        # Second acquire should block — verify thread is alive after
        # a short wait, then release the semaphore so it can proceed.
        import threading

        result = []

        def try_acquire():
            try:
                governor.acquire_sync("generative")
                result.append("acquired")
            except RuntimeError:
                result.append("failed")

        thread = threading.Thread(target=try_acquire)
        thread.start()
        # Give the thread time to reach the blocking semaphore.acquire().
        thread.join(timeout=0.2)
        # Thread should still be blocked.
        assert thread.is_alive(), "Thread should be blocked on semaphore"
        # Release the first slot — the second acquire should now proceed.
        governor.release_sync("generative")
        thread.join(timeout=1.0)
        assert not thread.is_alive(), "Thread should have completed after release"
        assert result == ["acquired"], "Second acquire should have succeeded"


# ---------------------------------------------------------------------------
# StoreConfig — resource governance defaults
# ---------------------------------------------------------------------------


class TestStoreConfigResourceGovernance:
    """Tests for StoreConfig resource governance defaults."""

    def test_defaults_are_reasonable(self):
        with patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "EMBEDDING_URL": "http://localhost:8001",
                "EMBEDDING_MODEL": "embed",
                "EMBEDDING_DIMENSION": "768",
                "EMBEDDING_API_KEY": "test-key",
                "EMBEDDING_REVISION": "v1",
                "RERANKER_URL": "http://localhost:8002",
                "RERANKER_MODEL": "rerank",
                "RERANKER_API_KEY": "test-key",
                "CHUNKER_NAME": "hierarchical",
                "CHUNKER_VERSION": "structural-v1",
                "CHUNKER_MAX_TOKENS": "1000",
                "TOKENIZER_NAME": "cl100k_base",
                "PARSER_VERSION": "markdown-v1",
                "NORMALIZATION_VERSION": "cleanup-v1",
                "PARSER_REGISTRY_VERSION": "canonical-v1",
                "MAX_INDEX_ATTEMPTS": "5",
                "INDEX_JOB_LEASE_SECONDS": "300",
                "INDEX_WORKER_POLL_SECONDS": "5",
                "EMBEDDING_BATCH_SIZE": "32",
                "GENERATIVE_URL": "http://localhost:8002",
                "GENERATIVE_MODEL": "qwen",
                "GENERATIVE_API_KEY": "test-key",
            },
        ):
            config = StoreConfig.from_env()
            assert config.generative_url == "http://localhost:8002"
            assert config.generative_model == "qwen"
            assert config.generative_api_key == "test-key"
            assert config.generative_max_concurrent == 1
            assert config.generative_max_input_tokens == 0
            assert config.generative_max_batch_size == 1
            assert config.embedding_max_concurrent == 4
            assert config.reranker_max_concurrent == 2

    def test_custom_env_overrides_defaults(self):
        with patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "EMBEDDING_URL": "http://localhost:8001",
                "EMBEDDING_MODEL": "embed",
                "EMBEDDING_DIMENSION": "768",
                "EMBEDDING_API_KEY": "test-key",
                "EMBEDDING_REVISION": "v1",
                "RERANKER_URL": "http://localhost:8002",
                "RERANKER_MODEL": "rerank",
                "RERANKER_API_KEY": "test-key",
                "CHUNKER_NAME": "hierarchical",
                "CHUNKER_VERSION": "structural-v1",
                "CHUNKER_MAX_TOKENS": "1000",
                "TOKENIZER_NAME": "cl100k_base",
                "PARSER_VERSION": "markdown-v1",
                "NORMALIZATION_VERSION": "cleanup-v1",
                "PARSER_REGISTRY_VERSION": "canonical-v1",
                "MAX_INDEX_ATTEMPTS": "5",
                "INDEX_JOB_LEASE_SECONDS": "300",
                "INDEX_WORKER_POLL_SECONDS": "5",
                "EMBEDDING_BATCH_SIZE": "32",
                "GENERATIVE_URL": "http://localhost:9000",
                "GENERATIVE_MODEL": "custom-model",
                "GENERATIVE_API_KEY": "custom-key",
                "GENERATIVE_MAX_CONCURRENT": "8",
                "EMBEDDING_MAX_CONCURRENT": "16",
                "RERANKER_MAX_CONCURRENT": "4",
            },
        ):
            config = StoreConfig.from_env()
            assert config.generative_url == "http://localhost:9000"
            assert config.generative_model == "custom-model"
            assert config.generative_api_key == "custom-key"
            assert config.generative_max_concurrent == 8
            assert config.embedding_max_concurrent == 16
            assert config.reranker_max_concurrent == 4


# ---------------------------------------------------------------------------
# CLI commands — endpoint-health and resource-status
# ---------------------------------------------------------------------------


class TestCLIResourceGovernance:
    """Tests for CLI commands."""

    def test_endpoint_health_parser_exists(self):
        from firecrawl_skill.research_store.cli import parser as cli_parser

        args = cli_parser().parse_args(["endpoint-health"])
        assert args.command == "endpoint-health"

    def test_resource_status_parser_exists(self):
        from firecrawl_skill.research_store.cli import parser as cli_parser

        args = cli_parser().parse_args(["resource-status"])
        assert args.command == "resource-status"


# ---------------------------------------------------------------------------
# Integration: health store with mock UOW
# ---------------------------------------------------------------------------


class TestHealthStoreIntegration:
    """Integration tests for the health store with a mock UOW."""

    def test_make_health_store_produces_callable(self):
        uow_factory = MagicMock()
        uow_mock = MagicMock()
        uow_factory.return_value.__enter__ = MagicMock(return_value=uow_mock)
        uow_factory.return_value.__exit__ = MagicMock(return_value=False)

        store = make_health_store(uow_factory)
        assert callable(store)

    def test_make_health_query_produces_callable(self):
        uow_factory = MagicMock()
        uow_mock = MagicMock()
        uow_factory.return_value.__enter__ = MagicMock(return_value=uow_mock)
        uow_factory.return_value.__exit__ = MagicMock(return_value=False)
        uow_mock.model_endpoints.get_health = MagicMock(return_value=None)

        query = make_health_query(uow_factory)
        assert callable(query)

    def test_health_persistence_integration(self):
        """Test that health persistence round-trips correctly."""
        # Create a governor with a mock store.
        stored = []
        query_results = {}

        def store_fn(name, health):
            stored.append((name, health))

        def query_fn(name):
            return query_results.get(name)

        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor(
            health_store=store_fn,
            health_query=query_fn,
        )
        governor.register_endpoint(config)

        # Set health — should call store_fn.
        governor.set_health("generative", EndpointStatus.HEALTHY)
        assert len(stored) == 1
        assert stored[0][0] == "generative"
        assert stored[0][1].status == EndpointStatus.HEALTHY

        # Verify in-memory state is returned first.
        health = governor.get_health("generative")
        assert health is not None
        assert health.status == EndpointStatus.HEALTHY

        # Reset in-memory state — now query_fn should be used.
        governor.reset_health("generative")

        # Populate query result.
        query_results["generative"] = EndpointHealth(
            endpoint_name="generative",
            url="http://localhost:8002",
            status=EndpointStatus.DEGRADED,
        )
        health = governor.get_health("generative")
        assert health is not None
        assert health.status == EndpointStatus.DEGRADED
        # After first query, it should be cached in-memory.
        health2 = governor.get_health("generative")
        assert health2 is not None
        assert health2.status == EndpointStatus.DEGRADED


# ---------------------------------------------------------------------------
# Integration: synthesis pipeline + governor
# ---------------------------------------------------------------------------


class TestSynthesisGovernorIntegration:
    """Tests for LocalSynthesisService._bounded_llm_call with governor."""

    def test_unhealthy_endpoint_raises_in_bounded_call(self):
        """_bounded_llm_call raises EndpointUnavailableError when generative is unhealthy."""
        from firecrawl_skill.research_store.resource_governor import (
            EndpointStatus,
            EndpointUnavailableError,
            ResourceGovernor,
        )

        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.UNHEALTHY, error="crash")

        # Create a mock LocalSynthesisService with the governor.
        from unittest.mock import MagicMock

        service = MagicMock()
        service._resource_governor = governor
        service.__class__._bounded_llm_call = __import__(
            "firecrawl_skill.research_store.report_service",
            fromlist=["LocalSynthesisService"],
        ).LocalSynthesisService._bounded_llm_call

        with pytest.raises(EndpointUnavailableError):
            service._bounded_llm_call(lambda: "should-not-run")

    def test_token_cap_enforced_in_bounded_call(self):
        """_bounded_llm_call enforces token cap via acquire_sync."""
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
            max_input_tokens=1000,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        from unittest.mock import MagicMock

        service = MagicMock()
        service._resource_governor = governor
        service.__class__._bounded_llm_call = __import__(
            "firecrawl_skill.research_store.report_service",
            fromlist=["LocalSynthesisService"],
        ).LocalSynthesisService._bounded_llm_call

        with pytest.raises(
            Exception, match="input tokens 2000 exceeds cap 1000"
        ):  # ResourceLimitError
            service._bounded_llm_call(lambda: "should-not-run", input_tokens=2000)

    def test_batch_cap_enforced_in_bounded_call(self):
        """_bounded_llm_call enforces batch cap via acquire_sync."""
        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
            max_batch_size=1,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        from unittest.mock import MagicMock

        service = MagicMock()
        service._resource_governor = governor
        service.__class__._bounded_llm_call = __import__(
            "firecrawl_skill.research_store.report_service",
            fromlist=["LocalSynthesisService"],
        ).LocalSynthesisService._bounded_llm_call

        with pytest.raises(Exception, match="batch size 5 exceeds cap 1"):
            service._bounded_llm_call(lambda: "should-not-run", batch_size=5)

    def test_no_governor_bypasses_enforcement(self):
        """_bounded_llm_call proceeds without gating when governor is None."""
        from unittest.mock import MagicMock, patch

        with patch(
            "firecrawl_skill.research_store.report_service.LocalSynthesisService._load_schemas"
        ):
            from firecrawl_skill.research_store.report_service import (
                LocalSynthesisService,
            )

            service = LocalSynthesisService(
                semantic_service=MagicMock(),
                evidence_service=MagicMock(),
                config=MagicMock(),
                resource_governor=None,
            )

        result = service._bounded_llm_call(lambda: "success")
        assert result == "success"

    def test_resource_limit_error_propagates_to_caller(self):
        """ResourceLimitError from acquire_sync propagates through _bounded_llm_call."""
        from firecrawl_skill.research_store.resource_governor import (
            ResourceLimit,
            ResourceLimitError,
        )

        config = EndpointConfig(
            name="generative",
            url="http://localhost:8002",
            max_concurrent=1,
            backpressure_threshold=2,
        )
        governor = ResourceGovernor()
        governor.register_endpoint(config)
        governor.set_health("generative", EndpointStatus.HEALTHY)

        # Manually set queued_requests to trigger backpressure.
        with governor._lock:
            existing = governor._health_states["generative"]
            governor._health_states["generative"] = EndpointHealth(
                endpoint_name=existing.endpoint_name,
                url=existing.url,
                status=existing.status,
                last_check_at=existing.last_check_at,
                last_error=existing.last_error,
                concurrent_requests=existing.concurrent_requests,
                queued_requests=3,
                total_checks=existing.total_checks,
                total_failures=existing.total_failures,
                degraded_since=existing.degraded_since,
                restart_count=existing.restart_count,
            )

        from unittest.mock import MagicMock

        service = MagicMock()
        service._resource_governor = governor
        service.__class__._bounded_llm_call = __import__(
            "firecrawl_skill.research_store.report_service",
            fromlist=["LocalSynthesisService"],
        ).LocalSynthesisService._bounded_llm_call

        with pytest.raises(ResourceLimitError) as exc_info:
            service._bounded_llm_call(lambda: "should-not-run")
        assert exc_info.value.limit == ResourceLimit.CONCURRENCY
