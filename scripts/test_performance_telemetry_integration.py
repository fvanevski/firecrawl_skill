"""PostgreSQL integration tests for run-scoped performance telemetry.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database whose
name contains a standalone ``test`` segment, and set
RESEARCH_STORE_TEST_ALLOW_RESET to that exact database name.

These tests exercise the full telemetry lifecycle:
- Migration 0036 creates the telemetry tables
- Endpoint usage is recorded from semantic calls
- Cache events are recorded
- Resource samples are collected
- build_summary aggregates all telemetry
- Strict mode rejects empty/missing telemetry
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from research_store.postgres import connect, migrate
from research_store.release_benchmark import (
    MetricEngine,
    ReleaseBenchmarkConfig,
)
from research_store.telemetry_service import PerformanceTelemetryService

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture(scope="session")
def telemetry_database():
    """Prove migration 0036 creates telemetry tables."""
    from research_store.postgres import require_disposable_database_reset

    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migration_count = migrate(TEST_DSN)
    assert migration_count >= 36, (
        f"Expected at least migration 36, got {migration_count}"
    )
    # Verify telemetry tables exist.
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('run_performance_telemetry'), "
            "to_regclass('run_cache_events'), "
            "to_regclass('run_embedding_throughput'), "
            "to_regclass('run_resource_samples'), "
            "to_regclass('endpoint_usage_records')"
        )
        row = cursor.fetchone()
        assert row[0] is not None, "run_performance_telemetry table missing"
        assert row[1] is not None, "run_cache_events table missing"
        assert row[2] is not None, "run_embedding_throughput table missing"
        assert row[3] is not None, "run_resource_samples table missing"
        assert row[4] is not None, "endpoint_usage_records table missing"


@pytest.fixture
def telemetry_connection(telemetry_database):
    """Return a fresh connection for each test."""
    conn = connect(TEST_DSN)
    yield conn
    conn.close()


class TestTelemetryLifecycle:
    """Full telemetry lifecycle: record → aggregate → read."""

    def test_endpoint_usage_and_summary(self, telemetry_connection):
        """Record endpoint usage, build summary, read it back."""
        from uuid import uuid4 as gen_uuid

        run_id = gen_uuid()

        # Create a research run row (required for FK).
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, external_id, objective,
                   execution_mode, state, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, now(), now())""",
                (
                    str(run_id),
                    f"test_{gen_uuid().hex[:8]}",
                    "Test objective",
                    "agent_led",
                    "created",
                ),
            )
        telemetry_connection.commit()

        # Record endpoint usage.
        svc = PerformanceTelemetryService(telemetry_connection)
        from research_store.telemetry_service import EndpointUsageRecord

        call_id = gen_uuid()
        record = EndpointUsageRecord(
            run_id=str(run_id),
            call_id=str(call_id),
            endpoint_type="generative",
            provider="openai-compatible",
            model="llama-3.1-8b",
            model_revision="v1",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            source="endpoint",
        )
        svc.record_endpoint_usage(record)

        # Record cache event.
        svc.record_cache_event(
            run_id=run_id,
            stage="draft",
            event_type="lookup",
            key_hash="abc123",
            hit=True,
        )

        # Record resource sample.
        from research_domain.models import ResourceSample

        sample = ResourceSample(
            run_id=str(run_id),
            device_type="cpu",
            device_index=0,
            sample_type="cpu_percent",
            value=45.0,
            sample_at="2026-07-28T12:00:00+00:00",
            collector="psutil",
            sample_number=0,
            status="measured",
        )
        svc.record_resource_sample(sample)

        # Build summary.
        summary = svc.build_summary(run_id)
        assert summary.total_tokens == 150
        assert summary.token_source == "endpoint"
        assert summary.semantic_calls == 0  # No semantic_calls row
        assert summary.cache_lookups == 1
        assert summary.cache_hits == 1
        assert summary.cache_misses == 0
        assert summary.cpu_samples == 1
        assert summary.cpu_mean_percent == 45.0
        assert summary.strict_pass is False  # No embedding telemetry

    def test_strict_mode_rejects_empty_telemetry(self, telemetry_connection):
        """Strict mode must fail when telemetry tables exist but are empty."""
        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, external_id, objective,
                   execution_mode, state, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, now(), now())""",
                (
                    str(run_id),
                    f"test_{uuid4().hex[:8]}",
                    "Test objective",
                    "agent_led",
                    "created",
                ),
            )
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        # Strict mode should raise because token_source is unavailable.
        with pytest.raises(RuntimeError, match="Strict mode:"):
            engine.extract_performance_metrics(run_id, 0.0)

    def test_cache_stats_query(self, telemetry_connection):
        """Cache stats correctly count lookups, hits, and misses."""
        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, external_id, objective,
                   execution_mode, state, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, now(), now())""",
                (
                    str(run_id),
                    f"test_{uuid4().hex[:8]}",
                    "Test objective",
                    "agent_led",
                    "created",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)

        # Record 7 hits + 3 misses.
        for i in range(10):
            svc.record_cache_event(
                run_id=run_id,
                stage="draft",
                event_type="lookup",
                key_hash=f"hash_{i}",
                hit=(i < 7),
            )

        stats = svc.get_cache_stats(run_id)
        assert stats["lookups"] == 10
        assert stats["hits"] == 7
        assert stats["misses"] == 3


class TestLegacyFallback:
    """Tests for legacy fallback paths when telemetry tables are absent."""

    def test_legacy_cache_hit_rate(self, telemetry_connection):
        """Legacy cache hit rate from global semantic_cache."""
        from research_store.release_benchmark import MetricEngine

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection

        # Insert some cache entries.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache (id, key_hash, status, created_at)
                   VALUES (%s, %s, %s, now())""",
                (str(uuid4()), "abc123", "valid"),
            )
            cur.execute(
                """INSERT INTO semantic_cache (id, key_hash, status, created_at)
                   VALUES (%s, %s, %s, now())""",
                (str(uuid4()), "def456", "expired"),
            )

        rate, formula = engine._legacy_cache_hit_rate()
        assert rate == 0.5
        assert "semantic_cache" in formula

    def test_legacy_cpu_percent(self, telemetry_connection):
        """Legacy CPU percent from psutil."""
        from research_store.release_benchmark import MetricEngine

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection

        cpu_pct = engine._legacy_cpu_percent()
        assert 0.0 <= cpu_pct <= 100.0

    def test_legacy_gpu_memory(self, telemetry_connection):
        """Legacy GPU memory from NVML (may be None)."""
        from research_store.release_benchmark import MetricEngine

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection

        gpu_mem = engine._legacy_gpu_memory()
        # May be None if NVML is unavailable.
        assert gpu_mem is None or gpu_mem >= 0.0


class TestRunScopedCacheIsolation:
    """Two-run cache isolation test — issue #159.

    Verifies that two benchmark runs with different cache events produce
    isolated cache metrics.  Records for Run A must not alter Run B's result.
    """

    def test_two_runs_with_different_cache_events_are_isolated(
        self, telemetry_connection
    ):
        """Run A and Run B with different cache events remain isolated.

        Issue #159: Campaign A and Campaign B cache metrics must be
        reproducible from their own event sets.  Unrelated prior cache
        records cannot alter a benchmark run's metric.
        """
        from uuid import uuid4

        from research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )
        from research_store.telemetry_service import PerformanceTelemetryService

        run_a = uuid4()
        run_b = uuid4()

        svc_a = PerformanceTelemetryService(telemetry_connection)
        svc_b = PerformanceTelemetryService(telemetry_connection)

        # Run A: 5 lookups, 2 hits, 3 misses
        for i in range(5):
            hit = i < 2
            svc_a.record_cache_event(
                run_a, "draft", "lookup", f"key-a-{i}", "fp-a", hit
            )

        # Run B: 3 lookups, 0 hits, 3 misses (zero hit rate)
        for i in range(3):
            svc_b.record_cache_event(
                run_b, "draft", "lookup", f"key-b-{i}", "fp-b", False
            )

        # Also insert global semantic_cache entries that should NOT affect
        # either run in strict mode.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache (id, key_hash, status, created_at)
                   VALUES (%s, %s, %s, now())""",
                (str(uuid4()), "global-key", "valid"),
            )

        engine = MetricEngine(TEST_DSN)
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics_a = engine.extract_performance_metrics(run_a, 0)
        _, metrics_b = engine.extract_performance_metrics(run_b, 0)

        # Run A: 2/5 = 0.4 hit rate
        cache_a = next(m for m in metrics_a if m.name == "cache_hit_rate")
        assert cache_a.value == pytest.approx(0.4, abs=0.001)
        assert cache_a.status == MetricStatus.MEASURED
        assert cache_a.source.table == "run_cache_events"

        # Run B: 0/3 = 0.0 hit rate
        cache_b = next(m for m in metrics_b if m.name == "cache_hit_rate")
        assert cache_b.value == 0.0
        assert cache_b.status == MetricStatus.MEASURED
        assert cache_b.source.table == "run_cache_events"

        # The two runs must have different cache hit rates.
        assert cache_a.value != cache_b.value

    def test_no_scoped_lookups_yields_unavailable(self, telemetry_connection):
        """No scoped lookups yields unavailable/unevaluated.

        Issue #159: when there are no scoped lookups for a run, the cache
        metric must be UNAVAILABLE — not a borrowed global ratio.
        """
        from uuid import uuid4

        from research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Insert global cache entries but NO run-scoped events.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache (id, key_hash, status, created_at)
                   VALUES (%s, %s, %s, now())""",
                (str(uuid4()), "global-key", "valid"),
            )

        engine = MetricEngine(TEST_DSN)
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        assert cache_metric.status == MetricStatus.UNAVAILABLE
        assert cache_metric.value == 0.0
        assert cache_metric.source.table == "run_cache_events"
        assert "semantic_cache" not in cache_metric.source.table

    def test_global_cache_rows_do_not_affect_strict_metrics(self, telemetry_connection):
        """Global cache rows with no run association cannot affect strict metrics.

        Issue #159: unrelated prior cache records cannot alter a benchmark
        run's metric.
        """
        from uuid import uuid4

        from research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Insert many global cache entries with various statuses.
        with telemetry_connection.cursor() as cur:
            for i in range(20):
                status = "valid" if i % 3 == 0 else "expired"
                cur.execute(
                    """INSERT INTO semantic_cache (id, key_hash, status, created_at)
                       VALUES (%s, %s, %s, now())""",
                    (str(uuid4()), f"global-key-{i}", status),
                )

        engine = MetricEngine(TEST_DSN)
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # The global cache entries must NOT affect the strict metric.
        assert cache_metric.status == MetricStatus.UNAVAILABLE
        assert cache_metric.value == 0.0
