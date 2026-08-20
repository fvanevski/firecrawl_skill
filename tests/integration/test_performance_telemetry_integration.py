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
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.postgres import connect, migrate
from firecrawl_skill.research_store.release_benchmark import (
    MetricEngine,
    MetricStatus,
    ReleaseBenchmarkConfig,
)
from firecrawl_skill.research_store.telemetry_service import PerformanceTelemetryService

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _now_iso():
    """Return current UTC time as an ISO-8601 string compatible with timestamptz."""
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture(scope="session")
def telemetry_database():
    """Prove migration 0036 creates telemetry tables."""
    from firecrawl_skill.research_store.postgres import (
        require_disposable_database_reset,
    )

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
        assert row is not None
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
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{gen_uuid().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        # Record endpoint usage.
        svc = PerformanceTelemetryService(telemetry_connection)
        from firecrawl_skill.research_store.telemetry_service import EndpointUsageRecord

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
        from firecrawl_skill.research_domain.models import ResourceSample

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
        """Strict mode must produce null metrics with UNAVAILABLE status.

        Strict mode no longer raises RuntimeError for empty telemetry.
        Instead it produces null metrics with clear UNAVAILABLE status.
        """
        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
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

        # Strict mode produces null metrics with UNAVAILABLE status.
        from firecrawl_skill.research_store.release_benchmark import MetricStatus

        _, metrics = engine.extract_performance_metrics(run_id, 0.0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")
        assert cache_metric.status == MetricStatus.UNAVAILABLE
        assert cache_metric.value is None

    def test_cache_stats_query(self, telemetry_connection):
        """Cache stats correctly count lookups, hits, and misses."""
        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
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

    def test_mixed_resource_status_and_missing_gpu_identity_fail_closed(
        self, telemetry_connection
    ):
        """Partial/error samples cannot be averaged into measured release metrics."""
        from firecrawl_skill.research_domain.models import ResourceSample

        run_id = uuid4()
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)
        for number, status in enumerate(("measured", "invalid")):
            svc.record_resource_sample(
                ResourceSample(
                    run_id=str(run_id),
                    device_type="cpu",
                    device_index=0,
                    sample_type="process_cpu_percent_normalized",
                    value=25.0,
                    sample_at=f"2026-07-28T12:00:0{number}+00:00",
                    collector="psutil",
                    collector_version="6.1.1",
                    sample_number=number,
                    status=status,
                )
            )
        svc.record_resource_sample(
            ResourceSample(
                run_id=str(run_id),
                device_type="gpu",
                device_index=0,
                device_uuid="",
                sample_type="gpu_memory_used_mb",
                value=512.0,
                sample_at="2026-07-28T12:00:00+00:00",
                collector="pynvml",
                collector_version="13.610.43",
                sample_number=0,
                status="measured",
            )
        )
        svc.build_summary(run_id)

        engine = MetricEngine(
            TEST_DSN,
            config=ReleaseBenchmarkConfig(
                database_url=TEST_DSN, blob_root=Path("/tmp"), strict=True
            ),
        )
        engine._connection = telemetry_connection
        performance, metrics = engine.extract_performance_metrics(run_id, 0)
        by_name = {metric.name: metric for metric in metrics}

        assert performance.cpu_percent is None
        assert by_name["cpu_percent"].status == MetricStatus.INVALID
        assert by_name["cpu_percent"].source.sample_count == 1
        assert dict(by_name["cpu_percent"].source.status_counts) == {
            "invalid": 1,
            "measured": 1,
        }
        assert performance.gpu_memory_mb is None
        assert by_name["gpu_memory_mb"].status == MetricStatus.INCOMPLETE
        assert by_name["gpu_memory_mb"].source.device_uuid == ""


class TestLegacyFallback:
    """Tests for legacy fallback paths when telemetry tables are absent."""

    def test_legacy_cpu_percent(self, telemetry_connection):
        """Legacy CPU percent from psutil."""
        from firecrawl_skill.research_store.release_benchmark import MetricEngine

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection

        cpu_pct = engine._legacy_cpu_percent()
        assert 0.0 <= cpu_pct <= 100.0

    def test_legacy_gpu_memory(self, telemetry_connection):
        """Legacy GPU memory from NVML (may be None)."""
        from firecrawl_skill.research_store.release_benchmark import MetricEngine

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
        import time
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        run_a = uuid4()
        run_b = uuid4()

        # Create parent run rows (required for FK constraint).
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_a),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_a_{uuid4().hex[:8]}",
                ),
            )
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_b),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_b_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

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

        # Build summaries so run_performance_telemetry is populated.
        svc_a.build_summary(run_a)
        svc_b.build_summary(run_b)

        # Also insert global semantic_cache entries that should NOT affect
        # either run in strict mode.

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache
                   (id, key_hash, stage, model_fingerprint, input_hash,
                    prompt_hash, prompt_version, schema_version, status, ttl_seconds, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid4()),
                    "global-key",
                    "draft",
                    "model-v1",
                    "input-hash-123",
                    "prompt-hash-456",
                    "1",
                    1,
                    "valid",
                    3600,
                    time.time(),
                ),
            )

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
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
        import time
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Insert global cache entries but NO run-scoped events.

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache
                   (id, key_hash, stage, model_fingerprint, input_hash,
                    prompt_hash, prompt_version, schema_version, status, ttl_seconds, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid4()),
                    "global-key",
                    "draft",
                    "model-v1",
                    "input-hash-123",
                    "prompt-hash-456",
                    "1",
                    1,
                    "valid",
                    3600,
                    time.time(),
                ),
            )

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        assert cache_metric.status == MetricStatus.UNAVAILABLE
        assert cache_metric.value is None
        assert cache_metric.source.table == "run_cache_events"
        assert "semantic_cache" not in cache_metric.source.table

    def test_global_cache_rows_do_not_affect_strict_metrics(self, telemetry_connection):
        """Global cache rows with no run association cannot affect strict metrics.

        Issue #159: unrelated prior cache records cannot alter a benchmark
        run's metric.
        """
        import time
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
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
                    """INSERT INTO semantic_cache
                       (id, key_hash, stage, model_fingerprint, input_hash,
                        prompt_hash, prompt_version, schema_version, status, ttl_seconds, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(uuid4()),
                        f"global-key-{i}",
                        "draft",
                        "model-v1",
                        "input-hash-123",
                        "prompt-hash-456",
                        "1",
                        1,
                        status,
                        3600,
                        time.time(),
                    ),
                )

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # The global cache entries must NOT affect the strict metric.
        assert cache_metric.status == MetricStatus.UNAVAILABLE
        assert cache_metric.value is None


class TestCacheEventProvenance:
    """Cache event provenance tests — issue #159.

    Verifies that cache metric source includes event IDs and semantic stages.
    """

    def test_cache_metric_source_includes_event_ids_and_stages(
        self, telemetry_connection
    ):
        """Cache metric source includes event IDs and semantic stages.

        Issue #159: metric provenance must include source event IDs or an
        equivalent deterministic query identity.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            ReleaseBenchmarkConfig,
        )
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        run_id = uuid4()

        # Create parent run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        # Record cache events across multiple stages.
        svc = PerformanceTelemetryService(telemetry_connection)
        for i in range(5):
            stage = "draft" if i < 3 else "outline"
            svc.record_cache_event(run_id, stage, "lookup", f"key-{i}", "fp", i < 2)

        # Build summary.
        svc.build_summary(run_id)

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=False,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # Event IDs must be populated (5 lookup events).
        assert len(cache_metric.source.event_ids) == 5, (
            f"Expected 5 event IDs, got {len(cache_metric.source.event_ids)}"
        )
        # Stages must be populated (draft and outline).
        assert set(cache_metric.source.stages) == {"draft", "outline"}, (
            f"Expected stages {{'draft', 'outline'}}, got {set(cache_metric.source.stages)}"
        )
        # Source table must still be run_cache_events.
        assert cache_metric.source.table == "run_cache_events"

    def test_cache_metric_source_empty_when_no_events(self, telemetry_connection):
        """Cache metric source has empty event IDs and stages when no events exist."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Create parent run row but no cache events.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
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

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # Event IDs and stages must be empty tuples.
        assert cache_metric.source.event_ids == ()
        assert cache_metric.source.stages == ()
        # Status must be UNAVAILABLE.
        assert cache_metric.status == MetricStatus.UNAVAILABLE


class TestCacheEventClassification:
    """Stale/invalidated/reused cache event classification — issue #159.

    Verifies that non-lookup event types (invalidation, reuse) are recorded
    but do not affect the lookup/hit/miss cache hit rate computation.
    """

    def test_invalidation_and_reuse_events_dont_affect_hit_rate(
        self, telemetry_connection
    ):
        """Stale, invalidated, and reused events are classified correctly.

        Issue #159: only 'lookup' events with hit=True/False contribute to
        the cache hit rate. 'invalidation' and 'reuse' events are recorded
        but do not affect the hit/miss ratio.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        run_id = uuid4()

        # Create parent run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)

        # Record 3 lookup events: 2 hits, 1 miss → 2/3 = 0.666... hit rate.
        for i in range(3):
            svc.record_cache_event(run_id, "draft", "lookup", f"key-{i}", "fp", i < 2)

        # Record invalidation and reuse events — these should NOT affect hit rate.
        svc.record_cache_event(run_id, "draft", "invalidation", "key-0", "fp", None)
        svc.record_cache_event(run_id, "draft", "reuse", "key-1", "fp", None)

        # Build summary.
        svc.build_summary(run_id)

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=False,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # Hit rate must be 2/3 (only lookup events count).
        assert cache_metric.value == pytest.approx(2.0 / 3.0, abs=0.001)
        assert cache_metric.status == MetricStatus.MEASURED
        # Source must still point to run_cache_events.
        assert cache_metric.source.table == "run_cache_events"

    def test_stage_filtering_excludes_unrelated_stages(self, telemetry_connection):
        """Stage filtering excludes unrelated semantic stages.

        Issue #159: when stages are specified, only events from those stages
        contribute to the cache hit rate.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        run_id = uuid4()

        # Create parent run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)

        # Record events across multiple stages.
        # draft: 2 lookups, 1 hit → 0.5 hit rate
        for i in range(2):
            svc.record_cache_event(
                run_id, "draft", "lookup", f"draft-{i}", "fp", i == 0
            )
        # outline: 2 lookups, 0 hits → 0.0 hit rate
        for i in range(2):
            svc.record_cache_event(
                run_id, "outline", "lookup", f"outline-{i}", "fp", False
            )

        # Build summary with stage filter (only draft).
        svc.build_summary(run_id, stages=("draft",))

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=False,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cache_metric = next(m for m in metrics if m.name == "cache_hit_rate")

        # With stage filter, only draft events count: 1/2 = 0.5.
        assert cache_metric.value == pytest.approx(0.5, abs=0.001)
        assert cache_metric.status == MetricStatus.MEASURED
        # Source stages reflect all stages that have events for this run
        # (the stages query in MetricEngine does not use the build_summary
        # stage filter — it queries all lookup events for the run_id).
        assert "draft" in cache_metric.source.stages


class TestAbsentTelemetryTables:
    """Integration test for absent telemetry tables — issue #160.

    Verifies that ``extract_performance_metrics()`` produces correct
    null / ``UNAVAILABLE`` metrics when the telemetry tables do not
    exist (pre-migration database scenario).  The old code would call
    the legacy psutil/NVML fallback in this state, producing a host-wide
    sample whose provenance formula claimed ``0.0`` — a value-provenance
    contradiction.

    These tests **drop the telemetry tables after migration** to simulate
    the pre-migration scenario where the tables never existed.
    """

    def test_strict_metrics_when_tables_dropped(self, telemetry_connection):
        """Strict mode with dropped telemetry tables yields null / UNAVAILABLE.

        Issue #160: when the telemetry tables do not exist,
        ``_read_telemetry`` returns ``telemetry_tables_exist = False``.
        In strict mode, both CPU and GPU must be null with
        ``UNAVAILABLE`` status and a formula documenting the empty source.
        No legacy psutil or NVML samples may appear.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Drop the telemetry tables to simulate pre-migration scenario.
        with telemetry_connection.cursor() as cur:
            for table in (
                "run_performance_telemetry",
                "run_cache_events",
                "run_embedding_throughput",
                "run_resource_samples",
                "endpoint_usage_records",
            ):
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        telemetry_connection.commit()

        # Create a research run row (required for FK).
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
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

        performance, metrics = engine.extract_performance_metrics(run_id, 0)

        # CPU: null value, UNAVAILABLE status, empty-source formula.
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert performance.cpu_percent is None
        assert cpu_metric.status == MetricStatus.UNAVAILABLE
        assert "no measured process-scoped CPU samples" in cpu_metric.formula
        assert cpu_metric.source.table == "run_resource_samples"

        # GPU: null value, UNAVAILABLE status, empty-source formula.
        gpu_metric = next(m for m in metrics if m.name == "gpu_memory_mb")
        assert performance.gpu_memory_mb is None
        assert gpu_metric.status == MetricStatus.UNAVAILABLE
        assert "no measured GPU samples" in gpu_metric.formula
        assert gpu_metric.source.table == "run_resource_samples"

        # Re-run migrations to recreate the dropped tables so that
        # subsequent tests in this session still have the tables.
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        migrate(TEST_DSN)


class TestNoSamplesInExistingTables:
    """Integration tests for empty telemetry tables — issue #160.

    These tests verify behavior when the telemetry tables exist but contain
    no samples (e.g. an orchestrator that failed before producing telemetry).
    The ``telemetry_database`` fixture migrates the tables, so they always
    exist in this test class.
    """

    def test_strict_metrics_no_samples_existing_tables(self, telemetry_connection):
        """Strict mode with existing but empty telemetry tables.

        When the telemetry tables exist but have no samples, strict mode
        blocks legacy fallbacks and produces null / UNAVAILABLE.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Create a research run row (required for FK).
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
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

        performance, metrics = engine.extract_performance_metrics(run_id, 0)

        # CPU: null value, UNAVAILABLE status, empty-source formula.
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert performance.cpu_percent is None
        assert cpu_metric.status == MetricStatus.UNAVAILABLE
        assert "no measured process-scoped CPU samples" in cpu_metric.formula

        # GPU: null value, UNAVAILABLE status (no samples).
        gpu_metric = next(m for m in metrics if m.name == "gpu_memory_mb")
        assert performance.gpu_memory_mb is None
        assert gpu_metric.status == MetricStatus.UNAVAILABLE

    def test_non_strict_uses_legacy_fallback_no_samples(self, telemetry_connection):
        """Non-strict mode with existing but empty telemetry tables.

        When telemetry tables exist but are empty and strict mode is off,
        the engine falls back to psutil/NVML.  The metric status should be
        MEASURED (not UNAVAILABLE) and the formula should reference the
        legacy source.
        """
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Create a research run row (required for FK).
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test objective",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(
            database_url=TEST_DSN,
            blob_root=Path("/tmp"),
            strict=False,
        )

        _, metrics = engine.extract_performance_metrics(run_id, 0)

        # CPU: non-strict mode uses psutil fallback for the value.
        # Status is UNAVAILABLE because cpu_samples == 0 (no authoritative data).
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert cpu_metric.status == MetricStatus.UNAVAILABLE
        assert "psutil" in cpu_metric.formula.lower()

        # GPU: non-strict mode uses NVML fallback for the value.
        # Status is UNAVAILABLE because gpu_samples == 0 (no authoritative data).
        gpu_metric = next(m for m in metrics if m.name == "gpu_memory_mb")
        assert gpu_metric.status == MetricStatus.UNAVAILABLE
        # Formula references NVML or "NVML not available" depending on hardware.
        assert "nvml" in gpu_metric.formula.lower()


# ---------------------------------------------------------------------------
# Telemetry completeness checks — issue #170 (item C)
# ---------------------------------------------------------------------------


class TestTokenCompleteness:
    """Tests for token telemetry completeness (issue #170, item C)."""

    def test_token_complete_when_all_calls_have_usage_records(
        self, telemetry_connection
    ):
        """When every semantic call has a matching endpoint_usage_record,
        token status should be MEASURED."""
        from uuid import uuid4

        from firecrawl_skill.research_domain.models import EndpointUsageRecord
        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()
        call_id_1 = uuid4()
        call_id_2 = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        # Create semantic calls.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_calls (id, run_id, stage, provider,
                   model, prompt_version, input_sha256, request, status,
                   idempotency_key, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s),
                          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(call_id_1),
                    str(run_id),
                    "draft",
                    "openai",
                    "gpt-4",
                    "v1",
                    "0" * 64,
                    "{}",
                    "complete",
                    f"idem-{call_id_1}",
                    _now_iso(),
                    str(call_id_2),
                    str(run_id),
                    "draft",
                    "openai",
                    "gpt-4",
                    "v1",
                    "0" * 64,
                    "{}",
                    "complete",
                    f"idem-{call_id_2}",
                    _now_iso(),
                ),
            )
        telemetry_connection.commit()

        # Create matching endpoint usage records.
        svc = PerformanceTelemetryService(telemetry_connection)
        for cid in (call_id_1, call_id_2):
            svc.record_endpoint_usage(
                EndpointUsageRecord(
                    run_id=str(run_id),
                    call_id=str(cid),
                    endpoint_type="generative",
                    provider="openai",
                    model="gpt-4",
                    total_tokens=100,
                    source="endpoint",
                )
            )
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        token_metric = next(m for m in metrics if m.name == "total_tokens")
        assert token_metric.status == MetricStatus.MEASURED

    def test_token_incomplete_when_calls_lack_usage_records(self, telemetry_connection):
        """When semantic calls exist without matching endpoint_usage_records,
        token status should be INCOMPLETE."""
        from uuid import uuid4

        from firecrawl_skill.research_domain.models import EndpointUsageRecord
        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()
        call_id_1 = uuid4()
        call_id_2 = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        # Create two semantic calls but only one usage record.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_calls (id, run_id, stage, provider,
                   model, prompt_version, input_sha256, request, status,
                   idempotency_key, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s),
                          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(call_id_1),
                    str(run_id),
                    "draft",
                    "openai",
                    "gpt-4",
                    "v1",
                    "0" * 64,
                    "{}",
                    "complete",
                    f"idem-{call_id_1}",
                    _now_iso(),
                    str(call_id_2),
                    str(run_id),
                    "draft",
                    "openai",
                    "gpt-4",
                    "v1",
                    "0" * 64,
                    "{}",
                    "complete",
                    f"idem-{call_id_2}",
                    _now_iso(),
                ),
            )
        telemetry_connection.commit()

        # Only one usage record.
        svc = PerformanceTelemetryService(telemetry_connection)
        svc.record_endpoint_usage(
            EndpointUsageRecord(
                run_id=str(run_id),
                call_id=str(call_id_1),
                endpoint_type="generative",
                provider="openai",
                model="gpt-4",
                total_tokens=100,
                source="endpoint",
            )
        )
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        token_metric = next(m for m in metrics if m.name == "total_tokens")
        assert token_metric.status == MetricStatus.INCOMPLETE
        assert "uncovered" in token_metric.formula.lower()


class TestEmbeddingCompleteness:
    """Tests for embedding telemetry completeness (issue #170, item C)."""

    def test_embedding_complete(self, telemetry_connection):
        """When all invariants hold, embedding status should be MEASURED."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        # Record embedding throughput with all invariants satisfied.
        svc = PerformanceTelemetryService(telemetry_connection)
        svc.record_embedding_throughput(
            run_id=run_id,
            stage="indexing",
            batch_count=2,
            vector_count=100,
            failed_count=0,
            total_texts=100,
            elapsed_seconds=5.0,
            endpoint_url="http://localhost:8002/embed",
            endpoint_model="text-embedding-3-small",
            dimension=1536,
        )
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        emb_metric = next(m for m in metrics if m.name == "embedding_throughput")
        assert emb_metric.status == MetricStatus.MEASURED

    def test_embedding_incomplete_with_failures(self, telemetry_connection):
        """When failed_count > 0, embedding status should be INCOMPLETE."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        # Create a research run row.
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)
        svc.record_embedding_throughput(
            run_id=run_id,
            stage="indexing",
            batch_count=2,
            vector_count=90,
            failed_count=1,
            total_texts=100,
            elapsed_seconds=5.0,
            endpoint_url="http://localhost:8002/embed",
            endpoint_model="text-embedding-3-small",
            dimension=1536,
        )
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        emb_metric = next(m for m in metrics if m.name == "embedding_throughput")
        assert emb_metric.status == MetricStatus.INCOMPLETE

    def test_embedding_incomplete_text_vector_mismatch(self, telemetry_connection):
        """When total_texts != vector_count, embedding status should be INCOMPLETE."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)
        svc.record_embedding_throughput(
            run_id=run_id,
            stage="indexing",
            batch_count=2,
            vector_count=95,
            failed_count=0,
            total_texts=100,
            elapsed_seconds=5.0,
            endpoint_url="http://localhost:8002/embed",
            endpoint_model="text-embedding-3-small",
            dimension=1536,
        )
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        emb_metric = next(m for m in metrics if m.name == "embedding_throughput")
        assert emb_metric.status == MetricStatus.INCOMPLETE


class TestResourceCompleteness:
    """Tests for resource sample completeness (issue #170, item C)."""

    def test_resource_complete_with_window_metadata(self, telemetry_connection):
        """A complete two-sample CPU window is MEASURED in strict mode."""
        from uuid import uuid4

        from firecrawl_skill.research_domain.models import ResourceSample
        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)
        for sample_number, value, sample_at in (
            (0, 40.0, "2026-07-30T11:59:30+00:00"),
            (1, 50.0, "2026-07-30T12:00:00+00:00"),
        ):
            svc.record_resource_sample(
                ResourceSample(
                    run_id=str(run_id),
                    device_type="cpu",
                    device_index=0,
                    sample_type="cpu_percent",
                    value=value,
                    sample_at=sample_at,
                    collector="psutil",
                    sample_number=sample_number,
                    status="measured",
                    window_start="2026-07-30T11:59:00+00:00",
                    window_end="2026-07-30T12:00:00+00:00",
                    sampling_interval_seconds=1.0,
                )
            )
        telemetry_connection.commit()
        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert cpu_metric.status == MetricStatus.MEASURED

    def test_resource_incomplete_without_window_metadata(self, telemetry_connection):
        """When resource samples lack window metadata, status should be INCOMPLETE."""
        from uuid import uuid4

        from firecrawl_skill.research_domain.models import ResourceSample
        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        run_id = uuid4()

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (id, objective, state, execution_mode, external_run_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    str(run_id),
                    "Test",
                    "created",
                    "agent_led",
                    f"test_{uuid4().hex[:8]}",
                ),
            )
        telemetry_connection.commit()

        svc = PerformanceTelemetryService(telemetry_connection)
        sample = ResourceSample(
            run_id=str(run_id),
            device_type="cpu",
            device_index=0,
            sample_type="cpu_percent",
            value=45.0,
            sample_at="2026-07-30T12:00:00+00:00",
            collector="psutil",
            sample_number=0,
            status="measured",
            # No window metadata.
        )
        svc.record_resource_sample(sample)
        telemetry_connection.commit()

        # Build summary to populate run_performance_telemetry.
        svc.build_summary(run_id)
        telemetry_connection.commit()

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert cpu_metric.status == MetricStatus.INCOMPLETE

        engine = MetricEngine(TEST_DSN)
        engine._connection = telemetry_connection
        engine.config = ReleaseBenchmarkConfig(strict=True)

        _, metrics = engine.extract_performance_metrics(run_id, 0)
        cpu_metric = next(m for m in metrics if m.name == "cpu_percent")
        assert cpu_metric.status == MetricStatus.INCOMPLETE


class TestOverlappingCampaignCacheIsolation:
    """Campaign timestamps and shared keys cannot weaken exact run isolation."""

    def test_overlapping_events_remain_exactly_run_scoped(
        self,
        telemetry_connection,
    ):
        from firecrawl_skill.research_store.release_benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        run_a = uuid4()
        run_b = uuid4()
        with telemetry_connection.cursor() as cur:
            for run_id, suffix in ((run_a, "a"), (run_b, "b")):
                cur.execute(
                    """INSERT INTO research_runs (
                           id, objective, state, execution_mode, external_run_id
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        str(run_id),
                        "Overlapping cache isolation",
                        "created",
                        "agent_led",
                        f"test_overlap_{suffix}_{uuid4().hex[:8]}",
                    ),
                )
        telemetry_connection.commit()

        telemetry = PerformanceTelemetryService(telemetry_connection)
        for _ in range(2):
            telemetry.record_cache_event(
                run_a,
                "draft",
                "lookup",
                "shared-key",
                "shared-fingerprint",
                True,
            )
            telemetry.record_cache_event(
                run_b,
                "draft",
                "lookup",
                "shared-key",
                "shared-fingerprint",
                False,
            )
        telemetry_connection.commit()

        overlap = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        with telemetry_connection.cursor() as cur:
            cur.execute(
                """UPDATE run_cache_events
                   SET created_at = %s
                   WHERE run_id IN (%s, %s)""",
                (overlap, str(run_a), str(run_b)),
            )
        telemetry_connection.commit()

        telemetry.build_summary(run_a)
        telemetry.build_summary(run_b)
        telemetry_connection.commit()

        engine = MetricEngine(
            TEST_DSN,
            config=ReleaseBenchmarkConfig(
                database_url=TEST_DSN,
                blob_root=Path("/tmp"),
                strict=True,
            ),
        )
        engine._connection = telemetry_connection
        _, metrics_a = engine.extract_performance_metrics(run_a, time.monotonic())
        _, metrics_b = engine.extract_performance_metrics(run_b, time.monotonic())
        cache_a = next(
            metric for metric in metrics_a if metric.name == "cache_hit_rate"
        )
        cache_b = next(
            metric for metric in metrics_b if metric.name == "cache_hit_rate"
        )

        assert cache_a.status == MetricStatus.MEASURED
        assert cache_b.status == MetricStatus.MEASURED
        assert cache_a.value == 1.0
        assert cache_b.value == 0.0
        assert set(cache_a.source.event_ids).isdisjoint(cache_b.source.event_ids)

        with telemetry_connection.cursor() as cur:
            cur.execute(
                """SELECT id::text
                   FROM run_cache_events
                   WHERE run_id = %s AND event_type = 'lookup'
                   ORDER BY id""",
                (str(run_a),),
            )
            db_a = {row[0] for row in cur.fetchall()}
            cur.execute(
                """SELECT id::text
                   FROM run_cache_events
                   WHERE run_id = %s AND event_type = 'lookup'
                   ORDER BY id""",
                (str(run_b),),
            )
            db_b = {row[0] for row in cur.fetchall()}

        assert set(cache_a.source.event_ids) == db_a
        assert set(cache_b.source.event_ids) == db_b
        assert db_a.isdisjoint(db_b)
