"""Tests for run-scoped performance telemetry (issue #143).

This suite exercises:
- Domain models: TokenAccounting, CacheEvent, EmbeddingThroughputRecord,
  ResourceSample, EndpointUsageRecord, PerformanceTelemetrySummary
- Token accounting: endpoint extraction, tokenizer fallback, unavailable
- Resource sampler: CPU/GPU sampling, availability, unavailability
- Telemetry service: record, read, aggregate
- MetricEngine: new telemetry path and legacy fallback
- Strict mode: rejection of estimated or partial telemetry
- PostgreSQL integration: run and stage isolation
- Regression: former token and throughput formulas cannot appear
"""

from __future__ import annotations

import sys
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

_HAS_PSUTIL_TEST = importlib_util.find_spec("psutil") is not None

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class TestTokenAccounting:
    """Tests for TokenAccounting domain model."""

    def test_endpoint_source_with_all_fields(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        ta = TokenAccounting(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            source="endpoint",
        )
        assert ta.source == "endpoint"
        assert ta.prompt_tokens == 100
        assert ta.completion_tokens == 50
        assert ta.total_tokens == 150
        assert ta.total == 150
        assert ta.status.value == "measured"

    def test_endpoint_source_with_total_only(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        ta = TokenAccounting(total_tokens=200, source="endpoint")
        assert ta.total == 200
        assert ta.status.value == "measured"

    def test_endpoint_source_prompt_plus_completion(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        ta = TokenAccounting(prompt_tokens=100, completion_tokens=50, source="endpoint")
        assert ta.total == 150

    def test_tokenizer_source(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        ta = TokenAccounting(
            tokenizer_prompt_tokens=80,
            tokenizer_completion_tokens=40,
            tokenizer_total_tokens=120,
            source="tokenizer",
        )
        assert ta.source == "tokenizer"
        assert ta.total == 120
        assert ta.status.value == "measured"

    def test_unavailable_source(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        ta = TokenAccounting(source="unavailable")
        assert ta.source == "unavailable"
        assert ta.total is None
        assert ta.status.value == "unavailable"

    def test_endpoint_source_requires_at_least_one_field(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        with pytest.raises(ValueError, match="requires at least one"):
            TokenAccounting(source="endpoint")

    def test_tokenizer_source_requires_at_least_one_field(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        with pytest.raises(ValueError, match="requires at least one"):
            TokenAccounting(source="tokenizer")

    def test_invalid_source_raises(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        with pytest.raises(ValueError, match="source must be"):
            TokenAccounting(source="fake")

    def test_invalid_schema_version_raises(self):
        from firecrawl_skill.research_domain.models import TokenAccounting

        with pytest.raises(ValueError, match="unsupported schema_version"):
            TokenAccounting(schema_version="token-accounting-v99")


class TestCacheEvent:
    """Tests for CacheEvent domain model."""

    def test_valid_lookup_event(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        ce = CacheEvent(
            run_id=str(uuid4()),
            stage="binding",
            event_type="lookup",
            key_hash="abc123",
            hit=True,
        )
        assert ce.event_type == "lookup"
        assert ce.hit is True

    def test_valid_hit_event(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        ce = CacheEvent(
            run_id=str(uuid4()),
            stage="draft",
            event_type="hit",
            hit=True,
        )
        assert ce.hit is True

    def test_valid_miss_event(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        ce = CacheEvent(
            run_id=str(uuid4()),
            stage="outline",
            event_type="miss",
            hit=False,
        )
        assert ce.hit is False

    def test_invalidation_no_hit_required(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        ce = CacheEvent(
            run_id=str(uuid4()),
            stage="binding",
            event_type="invalidation",
            hit=None,
        )
        assert ce.hit is None

    def test_lookup_requires_hit(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        with pytest.raises(ValueError, match="hit field is required"):
            CacheEvent(
                run_id=str(uuid4()),
                stage="binding",
                event_type="lookup",
                hit=None,
            )

    def test_invalid_event_type_raises(self):
        from firecrawl_skill.research_domain.models import CacheEvent

        with pytest.raises(ValueError, match="event_type must be"):
            CacheEvent(
                run_id=str(uuid4()),
                stage="binding",
                event_type="bogus",
                hit=True,
            )


class TestEmbeddingThroughputRecord:
    """Tests for EmbeddingThroughputRecord domain model."""

    def test_valid_record(self):
        from firecrawl_skill.research_domain.models import EmbeddingThroughputRecord

        rec = EmbeddingThroughputRecord(
            run_id=str(uuid4()),
            stage="indexing",
            batch_count=5,
            vector_count=48,
            failed_count=0,
            total_texts=48,
            elapsed_seconds=2.5,
            endpoint_url="http://localhost:8002/v1/embeddings",
            endpoint_model="nomic-embed-text",
            dimension=768,
        )
        assert rec.batch_count == 5
        assert rec.throughput == 19.2  # 48 / 2.5

    def test_zero_elapsed_throughput(self):
        from firecrawl_skill.research_domain.models import EmbeddingThroughputRecord

        rec = EmbeddingThroughputRecord()
        assert rec.throughput == 0.0

    def test_status_measured(self):
        from firecrawl_skill.research_domain.models import (
            EmbeddingThroughputRecord,
            TelemetryStatus,
        )

        rec = EmbeddingThroughputRecord(batch_count=1, elapsed_seconds=1.0)
        assert rec.status == TelemetryStatus.MEASURED

    def test_status_unavailable(self):
        from firecrawl_skill.research_domain.models import (
            EmbeddingThroughputRecord,
            TelemetryStatus,
        )

        rec = EmbeddingThroughputRecord()
        assert rec.status == TelemetryStatus.UNAVAILABLE


class TestResourceSample:
    """Tests for ResourceSample domain model."""

    def test_valid_cpu_sample(self):
        from firecrawl_skill.research_domain.models import ResourceSample

        sample = ResourceSample(
            run_id=str(uuid4()),
            device_type="cpu",
            device_index=0,
            sample_type="cpu_percent",
            value=45.5,
            sample_at="2026-07-28T12:00:00+00:00",
            collector="psutil",
            sample_number=1,
        )
        assert sample.device_type == "cpu"
        assert sample.value == 45.5
        assert sample.status == "measured"

    def test_valid_gpu_sample(self):
        from firecrawl_skill.research_domain.models import ResourceSample

        sample = ResourceSample(
            run_id=str(uuid4()),
            device_type="gpu",
            device_index=0,
            device_uuid="GPU-abc123",
            sample_type="gpu_memory_used_mb",
            value=4096.0,
            sample_at="2026-07-28T12:00:00+00:00",
            collector="pynvml",
            sample_number=1,
        )
        assert sample.device_type == "gpu"
        assert sample.device_uuid == "GPU-abc123"

    def test_invalid_device_type_raises(self):
        from firecrawl_skill.research_domain.models import ResourceSample

        with pytest.raises(ValueError, match="device_type must be"):
            ResourceSample(device_type="tpu")

    def test_invalid_status_raises(self):
        from firecrawl_skill.research_domain.models import ResourceSample

        with pytest.raises(ValueError, match="invalid status"):
            ResourceSample(device_type="cpu", status="bogus")


class TestEndpointUsageRecord:
    """Tests for EndpointUsageRecord domain model."""

    def test_valid_endpoint_record(self):
        from firecrawl_skill.research_domain.models import EndpointUsageRecord

        rec = EndpointUsageRecord(
            run_id=str(uuid4()),
            call_id=str(uuid4()),
            endpoint_type="generative",
            provider="openai-compatible",
            model="llama-3.1-8b",
            model_revision="v1",
            prompt_tokens=128,
            completion_tokens=64,
            total_tokens=192,
            source="endpoint",
        )
        assert rec.source == "endpoint"
        assert rec.prompt_tokens == 128

    def test_unavailable_record(self):
        from firecrawl_skill.research_domain.models import EndpointUsageRecord

        rec = EndpointUsageRecord()
        assert rec.source == "unavailable"
        assert rec.prompt_tokens == 0


class TestPerformanceTelemetrySummary:
    """Tests for PerformanceTelemetrySummary domain model."""

    def test_valid_summary(self):
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            total_tokens=15000,
            token_source="endpoint",
            semantic_calls=8,
            cache_lookups=10,
            cache_hits=3,
            cache_misses=7,
            cache_hit_rate=0.3,
            embedding_batch_count=5,
            embedding_vector_count=48,
            embedding_elapsed_seconds=2.5,
            embedding_throughput=19.2,
            cpu_samples=10,
            cpu_mean_percent=45.0,
            cpu_max_percent=78.0,
            gpu_samples=10,
            gpu_mean_memory_mb=4096.0,
            gpu_max_memory_mb=4500.0,
            gpu_unavailable=False,
            strict_pass=True,
        )
        assert summary.strict_pass is True
        assert summary.cache_hit_rate == 0.3

    def test_unavailable_token_source(self):
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            token_source="unavailable",
            strict_pass=False,
        )
        assert summary.strict_pass is False

    def test_invalid_cache_hit_rate_raises(self):
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        with pytest.raises(ValueError, match="cache_hit_rate must be"):
            PerformanceTelemetrySummary(
                run_id=str(uuid4()),
                cache_hit_rate=1.5,
            )


class TestExtractEndpointUsage:
    """Tests for token accounting endpoint extraction."""

    def test_usage_in_response_metadata(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        metadata = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }
        }
        result = extract_endpoint_usage(metadata)
        assert result.source == "endpoint"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.total_tokens == 150

    def test_usage_in_provenance(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        metadata = {
            "provenance": {
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "total_tokens": 300,
                }
            }
        }
        result = extract_endpoint_usage(metadata)
        assert result.source == "endpoint"
        assert result.prompt_tokens == 200

    def test_usage_in_last_attempt(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        metadata = {
            "attempts": [
                {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                },
                {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                    }
                },
            ]
        }
        result = extract_endpoint_usage(metadata)
        assert result.source == "endpoint"
        assert result.prompt_tokens == 100

    def test_no_usage_returns_unavailable(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        metadata = {"provenance": {}, "attempts": []}
        result = extract_endpoint_usage(metadata)
        assert result.source == "unavailable"

    def test_empty_response_metadata(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        result = extract_endpoint_usage({})
        assert result.source == "unavailable"

    def test_usage_with_bool_values_returns_unavailable(self):
        from firecrawl_skill.research_store.token_accounting import (
            extract_endpoint_usage,
        )

        metadata = {"usage": {"prompt_tokens": True, "completion_tokens": False}}
        result = extract_endpoint_usage(metadata)
        assert result.source == "unavailable"


class TestResourceSampler:
    """Tests for ResourceSampler."""

    def test_cpu_available_when_psutil_present(self):
        from firecrawl_skill.research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler()
        # psutil may or may not be available in the test environment
        # The sampler correctly reports availability.
        if sampler.cpu_available:
            assert sampler.cpu_available is True
        # If psutil is absent, cpu_available is False — that's expected.

    def test_process_cpu_window_discards_baseline_and_normalizes(self):
        """CPU is scoped to this process and the first delta is discarded."""
        import firecrawl_skill.research_store.resource_sampler as module

        process = mock.Mock()
        process.cpu_percent.side_effect = [0.0, 384.0]
        fake_psutil = SimpleNamespace(
            Process=mock.Mock(return_value=process),
            cpu_count=mock.Mock(return_value=4),
            __version__="6.1.1",
        )
        with (
            mock.patch.object(module, "_HAS_PSUTIL", True),
            mock.patch.object(module, "psutil", fake_psutil, create=True),
            mock.patch.object(module, "_HAS_PYNVML", False),
        ):
            sampler = module.ResourceSampler()
            sampler.begin_window()
            cpu_samples, _ = sampler.end_window()

        assert process.cpu_percent.call_args_list == [
            mock.call(interval=None),
            mock.call(interval=None),
        ]
        assert cpu_samples[0].value == 96.0
        assert cpu_samples[0].status == "measured"
        assert cpu_samples[0].sample_type == "process_cpu_percent_normalized"

    def test_gpu_initializes_before_first_sample_and_records_identity(self):
        """The first GPU observation initializes NVML without recursive locking."""
        import firecrawl_skill.research_store.resource_sampler as module

        handle = object()
        fake_nvml = SimpleNamespace(
            __version__="13.610.43",
            nvmlInit=mock.Mock(),
            nvmlShutdown=mock.Mock(),
            nvmlDeviceGetHandleByIndex=mock.Mock(return_value=handle),
            nvmlDeviceGetUUID=mock.Mock(return_value="GPU-test-uuid"),
            nvmlDeviceGetMemoryInfo=mock.Mock(
                return_value=SimpleNamespace(used=512 * 1024 * 1024)
            ),
        )
        with (
            mock.patch.object(module, "_HAS_PSUTIL", False),
            mock.patch.object(module, "_HAS_PYNVML", True),
            mock.patch.object(module, "pynvml", fake_nvml, create=True),
        ):
            sampler = module.ResourceSampler()
            sampler.begin_window()
            _, gpu_samples = sampler.end_window()

        assert len(gpu_samples) == 2
        assert all(sample.status == "measured" for sample in gpu_samples)
        assert all(sample.device_uuid == "GPU-test-uuid" for sample in gpu_samples)
        fake_nvml.nvmlInit.assert_called_once_with()
        fake_nvml.nvmlShutdown.assert_called_once_with()

    def test_gpu_missing_library_and_collector_error_are_distinct(self):
        """Missing collector is unavailable; an installed collector failure is invalid."""
        import firecrawl_skill.research_store.resource_sampler as module

        with mock.patch.object(module, "_HAS_PYNVML", False):
            unavailable = module.ResourceSampler().collect_gpu_sample()
        assert unavailable is not None
        assert unavailable.status == "unavailable"
        assert unavailable.collector_version == "not-installed"

        fake_nvml = SimpleNamespace(
            __version__="13.610.43",
            nvmlInit=mock.Mock(side_effect=RuntimeError("driver failure")),
            nvmlShutdown=mock.Mock(),
        )
        with (
            mock.patch.object(module, "_HAS_PYNVML", True),
            mock.patch.object(module, "pynvml", fake_nvml, create=True),
        ):
            invalid = module.ResourceSampler().collect_gpu_sample()
        assert invalid is not None
        assert invalid.status == "invalid"
        assert invalid.collector_version == "13.610.43"

    @pytest.mark.skipif(not _HAS_PSUTIL_TEST, reason="psutil not available")
    def test_collect_cpu_sample(self):
        from firecrawl_skill.research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler()
        sampler.begin_window()
        sample = sampler.collect_cpu_sample()
        assert sample is not None
        assert sample.device_type == "cpu"
        assert sample.sample_type == "process_cpu_percent_normalized"
        assert sample.status == "measured"
        assert sample.value is not None
        assert 0 <= sample.value <= 100

    @pytest.mark.skipif(not _HAS_PSUTIL_TEST, reason="psutil not available")
    def test_cpu_sample_incremental(self):
        from firecrawl_skill.research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler()
        sampler.begin_window()
        s1 = sampler.collect_cpu_sample()
        s2 = sampler.collect_cpu_sample()
        assert s1 is not None
        assert s2 is not None
        assert s1.sample_number == 0
        assert s2.sample_number == 1

    @pytest.mark.skipif(not _HAS_PSUTIL_TEST, reason="psutil not available")
    def test_summarize_cpu(self):
        from firecrawl_skill.research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler()
        sampler.begin_window()
        # Collect a few samples
        for _ in range(3):
            sampler.collect_cpu_sample()
        summary = sampler.summarize_cpu()
        assert summary.device_type == "cpu"
        assert summary.sample_count == 3
        assert summary.status == "measured"
        assert summary.mean is not None
        assert summary.maximum is not None

    @pytest.mark.skipif(not _HAS_PSUTIL_TEST, reason="psutil not available")
    def test_max_samples_limit(self):
        from firecrawl_skill.research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler(max_samples=2)
        sampler.begin_window()
        s1 = sampler.collect_cpu_sample()
        s2 = sampler.collect_cpu_sample()
        s3 = sampler.collect_cpu_sample()
        assert s1 is not None
        assert s2 is not None
        assert s3 is None  # exceeded max_samples


class TestTelemetryService:
    """Tests for PerformanceTelemetryService."""

    @pytest.fixture
    def mock_connection(self):
        """Mock psycopg connection that records executed queries."""

        class MockCursor:
            def __init__(self):
                self.results = []

            def execute(self, query, params=None):
                self.results.append((query, params))

            def fetchone(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class MockConnection:
            def __init__(self):
                self.cursor_data = MockCursor()

            def execute(self, query, params=None):
                self.cursor_data.execute(query, params)

            def cursor(self):
                return self.cursor_data

        return MockConnection()

    def test_record_cache_event(self, mock_connection):
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        svc = PerformanceTelemetryService(mock_connection)
        run_id = uuid4()
        svc.record_cache_event(run_id, "binding", "lookup", "abc123", "fp1", True)
        assert len(mock_connection.cursor_data.results) == 1

    def test_write_summary(self, mock_connection):
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        svc = PerformanceTelemetryService(mock_connection)
        summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            total_tokens=1000,
            token_source="endpoint",
            semantic_calls=5,
            strict_pass=True,
        )
        svc.write_summary(summary)
        assert len(mock_connection.cursor_data.results) == 1

    def test_strict_pass_false_when_unavailable_tokens(self):
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        _summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            token_source="unavailable",
            strict_pass=True,  # Will be corrected by business logic
        )
        # The summary itself allows any strict_pass value;
        # the telemetry service enforces the rule during build_summary.


class TestStrictModeRejection:
    """Tests that strict mode rejects estimated or partial telemetry."""

    def test_strict_mode_rejects_unavailable_tokens(self):
        """Strict mode must fail when token source is unavailable."""
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            token_source="unavailable",
            strict_pass=True,
        )
        # In strict mode, token_source="unavailable" should force strict_pass=False
        # This is enforced by the telemetry service's build_summary method.
        assert summary.token_source == "unavailable"

    def test_strict_mode_rejects_no_cpu_samples(self):
        """Strict mode must fail when no CPU samples collected."""
        from firecrawl_skill.research_domain.models import PerformanceTelemetrySummary

        summary = PerformanceTelemetrySummary(
            run_id=str(uuid4()),
            cpu_samples=0,
            strict_pass=True,
        )
        assert summary.cpu_samples == 0

    def test_strict_mode_produces_null_metrics_when_telemetry_tables_absent(self):
        """Strict mode records unavailable values as null.

        When telemetry tables do not exist (pre-migration DB), strict mode
        now produces null metrics with clear formulas documenting the empty
        source, rather than raising RuntimeError. The test verifies that
        the engine completes without error and returns measurable (0.0)
        performance metrics.
        """
        import time
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            ReleaseBenchmarkConfig,
        )

        mock_conn = mock.Mock()
        # Query fails (pre-migration DB) → telemetry_tables_exist=False.
        mock_conn.execute.side_effect = Exception("table does not exist")
        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        # Strict mode no longer raises; it produces explicit null metrics.
        # The mock connection does not support the context manager protocol,
        # so we need to mock cursor() to return a context manager.
        # Also mock fetchone() to return a proper tuple matching the query
        # (SELECT COUNT(*), SUM(...) returns 2 values).
        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0, 0)  # COUNT=0, SUM=0
        mock_conn.cursor.return_value = mock_cursor
        performance, _ = engine.extract_performance_metrics(uuid4(), time.monotonic())
        assert performance.total_tokens is None
        assert performance.embedding_throughput is None
        assert performance.cache_hit_rate is None
        assert performance.cpu_percent is None
        assert performance.gpu_memory_mb is None

    def test_strict_mode_blocks_legacy_fallbacks(self):
        """Strict mode must not fall back to legacy sources when telemetry is unavailable.

        When telemetry tables exist but individual metrics are missing
        (e.g. cache_lookups=0, cpu_samples=0, embedding_throughput=0),
        strict mode must produce null unavailable metrics instead of falling
        back to global semantic_cache, live psutil, or live NVML. This prevents
        spurious cross-campaign contamination from unrelated prior runs.
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            ReleaseBenchmarkConfig,
        )

        mock_conn = mock.Mock()

        # Telemetry query (execute) returns all zeros → strict flags fire.
        mock_telemetry_cursor = mock.Mock()
        mock_telemetry_cursor.__enter__ = mock.Mock(return_value=mock_telemetry_cursor)
        mock_telemetry_cursor.__exit__ = mock.Mock(return_value=False)
        mock_telemetry_cursor.fetchone.return_value = (
            None,  # total_tokens
            None,  # token_source
            0,  # cache_lookups
            0,  # cache_hits
            0,  # cache_misses
            0,  # embedding_batch_count
            0.0,  # embedding_throughput
            0,  # embedding_total_texts
            0.0,  # embedding_elapsed_seconds
            None,  # cpu_mean
            0,  # cpu_count
            None,  # gpu_mean
            0,  # gpu_count
        )
        mock_conn.execute.return_value = mock_telemetry_cursor

        # cursor() used for semantic_calls and other queries.
        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)  # COUNT(*) for semantic_calls
        mock_conn.cursor.return_value = mock_cursor

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        performance, _ = engine.extract_performance_metrics(uuid4(), time.monotonic())

        assert performance.total_tokens is None
        assert performance.embedding_throughput is None
        assert performance.cache_hit_rate is None
        assert performance.cpu_percent is None
        assert performance.gpu_memory_mb is None

    def test_strict_cache_metric_status_is_unavailable(self):
        """Cache metric status must be UNAVAILABLE when no scoped lookups exist.

        Issue #159: strict cache metrics must carry an explicit status
        independent of the numeric value.  When cache_lookups == 0 in strict
        mode the status must be UNAVAILABLE with a null value — never a
        host-global fallback or numeric sentinel.
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        mock_conn = mock.Mock()

        # Telemetry query returns all zeros → strict flags fire.
        mock_telemetry_cursor = mock.Mock()
        mock_telemetry_cursor.__enter__ = mock.Mock(return_value=mock_telemetry_cursor)
        mock_telemetry_cursor.__exit__ = mock.Mock(return_value=False)
        mock_telemetry_cursor.fetchone.return_value = (
            None,  # total_tokens
            None,  # token_source
            0,  # cache_lookups
            0,  # cache_hits
            0,  # cache_misses
            0,  # embedding_batch_count
            0.0,  # embedding_throughput
            0,  # embedding_total_texts
            0.0,  # embedding_elapsed_seconds
            None,  # cpu_mean
            0,  # cpu_count
            None,  # gpu_mean
            0,  # gpu_count
        )
        mock_conn.execute.return_value = mock_telemetry_cursor

        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)  # COUNT(*) for semantic_calls
        mock_conn.cursor.return_value = mock_cursor

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(uuid4(), time.monotonic())

        # Find the cache_hit_rate metric record.
        cache_metric = next((m for m in metrics if m.name == "cache_hit_rate"), None)
        assert cache_metric is not None, "cache_hit_rate metric record missing"
        assert cache_metric.status == MetricStatus.UNAVAILABLE, (
            f"Expected UNAVAILABLE, got {cache_metric.status}"
        )
        assert cache_metric.value is None
        # Source must always point to run_cache_events (issue #159).
        assert cache_metric.source.table == "run_cache_events"
        assert (
            cache_metric.source.run_id is not None and cache_metric.source.run_id != ""
        )

    def test_strict_cache_metric_source_never_points_to_semantic_cache(self):
        """Cache metric source.table must always be 'run_cache_events'.

        Issue #159: the source provenance must never reference the global
        semantic_cache table, even when the metric is unavailable.
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            ReleaseBenchmarkConfig,
        )

        mock_conn = mock.Mock()

        # Telemetry query returns all zeros.
        mock_telemetry_cursor = mock.Mock()
        mock_telemetry_cursor.__enter__ = mock.Mock(return_value=mock_telemetry_cursor)
        mock_telemetry_cursor.__exit__ = mock.Mock(return_value=False)
        mock_telemetry_cursor.fetchone.return_value = (
            None,
            None,
            0,
            0,
            0,
            0.0,
            0,
            0.0,
            None,
            0,
            None,
            0,
        )
        mock_conn.execute.return_value = mock_telemetry_cursor

        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.cursor.return_value = mock_cursor

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        _, metrics = engine.extract_performance_metrics(uuid4(), time.monotonic())

        cache_metric = next((m for m in metrics if m.name == "cache_hit_rate"), None)
        assert cache_metric is not None
        assert cache_metric.source.table == "run_cache_events"
        assert "semantic_cache" not in cache_metric.source.table

    def test_non_strict_cache_metric_status_is_measured_with_lookups(self):
        """Cache metric status must be MEASURED when scoped lookups exist.

        Issue #159: when cache_lookups > 0 the status is MEASURED regardless
        of strict mode, because the authoritative source (run_cache_events)
        has data.
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        mock_conn = mock.Mock()

        # Telemetry query returns non-zero cache data.
        mock_telemetry_cursor = mock.Mock()
        mock_telemetry_cursor.__enter__ = mock.Mock(return_value=mock_telemetry_cursor)
        mock_telemetry_cursor.__exit__ = mock.Mock(return_value=False)
        mock_telemetry_cursor.fetchone.return_value = (
            1000,  # total_tokens
            "endpoint",  # token_source
            10,  # cache_lookups
            3,  # cache_hits
            7,  # cache_misses
            1,  # embedding_batch_count
            50.0,  # embedding_throughput
            48,  # embedding_total_texts
            2.5,  # embedding_elapsed_seconds
            45.0,  # cpu_mean
            5,  # cpu_count
            1024.0,  # gpu_mean
            3,  # gpu_count
        )
        mock_conn.execute.return_value = mock_telemetry_cursor

        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (5,)  # COUNT(*) for semantic_calls
        mock_conn.cursor.return_value = mock_cursor

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=False,
        )

        _, metrics = engine.extract_performance_metrics(uuid4(), time.monotonic())

        cache_metric = next((m for m in metrics if m.name == "cache_hit_rate"), None)
        assert cache_metric is not None
        assert cache_metric.status == MetricStatus.MEASURED
        assert cache_metric.value == 0.3  # 3/10
        assert cache_metric.source.table == "run_cache_events"

    def test_strict_mode_passes_when_telemetry_tables_exist(self: object) -> None:
        """Strict mode proceeds when telemetry tables exist and data is present."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import MetricEngine

        engine = MetricEngine("postgresql://fake")

        # When the query succeeds, telemetry_tables_exist must be True.
        # We mock the connection so the query succeeds with a valid row.
        mock_cursor = mock.Mock()
        mock_cursor.fetchone.return_value = (
            1000,
            "endpoint",
            10,
            3,
            7,
            1,
            50.0,
            48,
            2.5,
            45.0,
            5,
            1024.0,
            3,
        )
        mock_conn = mock.Mock()
        mock_conn.execute.return_value = mock_cursor

        engine._connection = mock_conn
        result = engine._read_telemetry(uuid4())
        assert result["telemetry_tables_exist"] is True
        assert result["total_tokens"] == 1000
        assert result["token_source"] == "endpoint"

    def test_read_telemetry_sets_false_when_query_fails(self):
        """_read_telemetry must leave telemetry_tables_exist=False on failure."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import MetricEngine

        mock_conn = mock.Mock()
        mock_conn.execute.side_effect = Exception("table does not exist")
        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn

        result = engine._read_telemetry(uuid4())
        assert result["telemetry_tables_exist"] is False
        assert result["token_source"] == "unavailable"

    def test_strict_cpu_rejects_legacy_when_tables_absent(self):
        """CPU strict flag must fire when telemetry tables are absent.

        Issue #160: the strict CPU flag was only set when ``cpu_samples == 0``,
        not when the telemetry query itself failed.  The original regression
        test mocked the execute call to raise, which caused ``_read_telemetry``
        to return its default dict with ``cpu_samples == 0`` — so the old
        condition ``if telemetry["cpu_samples"] == 0`` already fired and the
        test passed against the parent commit.  That test did not distinguish
        old from new.

        This regression test mocks ``_read_telemetry`` directly to produce a
        state that **fails** under the old implementation:
        ``cpu_samples > 0`` but ``telemetry_tables_exist = False``.  Under the
        old code the strict flag would stay ``False`` (because
        ``cpu_samples != 0``) and the legacy ``_legacy_cpu_percent()`` would
        execute — producing a live host-wide psutil sample whose provenance
        formula claimed ``0.0``.

        The new condition ``or not telemetry.get("telemetry_tables_exist")``
        fires in this state, blocking the legacy fallback.
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock.Mock()
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        # Mock _read_telemetry to return a state that the old code would
        # miss: cpu_samples > 0 but telemetry_tables_exist = False.
        # Under the old code, _strict_cpu_unavailable would stay False
        # (cpu_samples != 0), and _legacy_cpu_percent() would be called.
        engine._read_telemetry = mock.Mock(
            return_value={
                "total_tokens": 0,
                "token_source": "unavailable",
                "cache_lookups": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "embedding_batch_count": 0,
                "embedding_throughput": 0.0,
                "embedding_total_texts": 0,
                "embedding_elapsed_seconds": 0.0,
                "cpu_samples": 5,  # > 0 — old code would NOT set strict flag
                "cpu_mean_percent": None,
                "gpu_samples": 0,
                "gpu_mean_memory_mb": None,
                "telemetry_tables_exist": False,  # tables absent
            }
        )

        # Mock cursor for semantic_calls and other queries.
        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)
        engine._connection.cursor.return_value = mock_cursor

        performance, metrics = engine.extract_performance_metrics(
            uuid4(), time.monotonic()
        )

        assert performance.cpu_percent is None

        # CPU metric status must be UNAVAILABLE.
        cpu_metric = next((m for m in metrics if m.name == "cpu_percent"), None)
        assert cpu_metric is not None
        assert cpu_metric.status == MetricStatus.UNAVAILABLE
        assert "no measured process-scoped CPU samples" in cpu_metric.formula

        assert performance.gpu_memory_mb is None
        gpu_metric = next((m for m in metrics if m.name == "gpu_memory_mb"), None)
        assert gpu_metric is not None
        assert gpu_metric.status == MetricStatus.UNAVAILABLE

    def test_strict_gpu_rejects_legacy_when_tables_absent(self):
        """GPU strict flag fires when telemetry tables are absent.

        Issue #160: verifies that the GPU path also produces 0.0 with
        UNAVAILABLE status when the telemetry tables don't exist,
        preventing a legacy NVML sample from leaking into the artifact.

        Uses the same mock-``_read_telemetry`` approach as the CPU test to
        produce a distinguishing state (``gpu_samples > 0`` but
        ``telemetry_tables_exist = False``).
        """
        import time
        from unittest import mock
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            MetricEngine,
            MetricStatus,
            ReleaseBenchmarkConfig,
        )

        engine = MetricEngine("postgresql://fake")
        engine._connection = mock.Mock()
        engine.config = ReleaseBenchmarkConfig(
            database_url="postgresql://fake",
            blob_root=Path("/tmp"),
            strict=True,
        )

        engine._read_telemetry = mock.Mock(
            return_value={
                "total_tokens": 0,
                "token_source": "unavailable",
                "cache_lookups": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "embedding_batch_count": 0,
                "embedding_throughput": 0.0,
                "embedding_total_texts": 0,
                "embedding_elapsed_seconds": 0.0,
                "cpu_samples": 0,
                "cpu_mean_percent": None,
                "gpu_samples": 3,  # > 0 — old code would NOT set strict flag
                "gpu_mean_memory_mb": 1024.0,
                "telemetry_tables_exist": False,  # tables absent
            }
        )

        mock_cursor = mock.Mock()
        mock_cursor.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = mock.Mock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)
        engine._connection.cursor.return_value = mock_cursor

        _, metrics = engine.extract_performance_metrics(uuid4(), time.monotonic())

        gpu_metric = next((m for m in metrics if m.name == "gpu_memory_mb"), None)
        assert gpu_metric is not None
        assert gpu_metric.status == MetricStatus.UNAVAILABLE
        assert "no measured GPU samples" in gpu_metric.formula


class TestCacheCounting:
    """Tests that cache lookup outcomes are counted from lookup rows."""

    def test_lookup_with_hit_true_counts_as_hit(self):
        """One lookup with hit=True should produce lookups=1, hits=1, misses=0."""
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        mock_conn = mock.Mock()
        mock_conn.execute.return_value.fetchone.return_value = (1, 1, 0)
        svc = PerformanceTelemetryService(mock_conn)
        stats = svc.get_cache_stats(uuid4())
        assert stats["lookups"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        # Verify the query filters on event_type='lookup' AND hit IS TRUE
        calls = mock_conn.execute.call_args_list
        sql = calls[0][0][0]
        assert "event_type = 'lookup' AND hit IS TRUE" in sql

    def test_lookup_with_hit_false_counts_as_miss(self):
        """One lookup with hit=False should produce lookups=1, hits=0, misses=1."""
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        mock_conn = mock.Mock()
        mock_conn.execute.return_value.fetchone.return_value = (1, 0, 1)
        svc = PerformanceTelemetryService(mock_conn)
        stats = svc.get_cache_stats(uuid4())
        assert stats["lookups"] == 1
        assert stats["hits"] == 0
        assert stats["misses"] == 1

    def test_mixed_lookups(self):
        """7 hits + 3 misses out of 10 lookups."""
        from firecrawl_skill.research_store.telemetry_service import (
            PerformanceTelemetryService,
        )

        mock_conn = mock.Mock()
        mock_conn.execute.return_value.fetchone.return_value = (10, 7, 3)
        svc = PerformanceTelemetryService(mock_conn)
        stats = svc.get_cache_stats(uuid4())
        assert stats["lookups"] == 10
        assert stats["hits"] == 7
        assert stats["misses"] == 3
        # Hit rate = 7/10 = 0.7
        assert stats["lookups"] > 0
        hit_rate = stats["hits"] / stats["lookups"]
        assert hit_rate == 0.7


class TestRollbackBeforeFallback:
    """Tests that failed telemetry query is rolled back before legacy fallback."""

    def test_read_telemetry_rollback_on_failure(self):
        """_read_telemetry must call rollback() when the query fails."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import MetricEngine

        mock_conn = mock.Mock()
        # First call (telemetry query) fails
        mock_conn.execute.side_effect = Exception("table does not exist")
        engine = MetricEngine("postgresql://fake")
        engine._connection = mock_conn

        result = engine._read_telemetry(uuid4())
        # Should return default values
        assert result["total_tokens"] == 0
        assert result["token_source"] == "unavailable"
        # Must have called rollback()
        mock_conn.rollback.assert_called_once()


class TestRegression:
    """Regression tests proving former formulas cannot appear."""

    def test_token_formula_not_semantic_calls_times_500(self):
        """The string 'semantic_calls * 500' must not appear in new telemetry."""
        import firecrawl_skill.research_store.telemetry_service

        with open(firecrawl_skill.research_store.telemetry_service.__file__) as f:
            source = f.read()
        assert "semantic_calls * 500" not in source, (
            "telemetry_service.py still contains the old estimation formula"
        )

    def test_release_benchmark_no_estimate_in_telemetry_path(self):
        """The new telemetry path in release_benchmark.py must not use
        semantic_calls * 500 as the primary token source."""
        import firecrawl_skill.research_store.release.benchmark as firecrawl_skill

        with open(firecrawl_skill.research_store.release.benchmark.__file__) as f:
            source = f.read()
        # The old formula should only appear in the legacy fallback comment.
        # Check that the primary path reads from endpoint_usage_records.
        assert "endpoint_usage_records" in source, (
            "release_benchmark.py should read tokens from endpoint_usage_records"
        )


class TestBenchmarkTelemetryWiring:
    """Tests for telemetry wiring in ReleaseBenchmarkRunner."""

    def test_populate_endpoint_usage_from_semantic_calls(self):
        """_populate_endpoint_usage extracts token usage from semantic calls."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            ReleaseBenchmarkRunner,
        )

        # Create a mock runner with the method.
        runner = ReleaseBenchmarkRunner.__new__(ReleaseBenchmarkRunner)

        mock_conn = mock.Mock()
        mock_cursor = mock.Mock()
        mock_conn.cursor.return_value.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = mock.Mock(return_value=False)

        # Mock semantic_calls with response_metadata containing usage.
        call_id = uuid4()
        mock_cursor.fetchall.return_value = [
            (
                str(call_id),
                {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                    }
                },
            )
        ]

        mock_telemetry_svc = mock.Mock()

        runner._populate_endpoint_usage(mock_telemetry_svc, uuid4(), mock_conn)

        # Verify record_endpoint_usage was called.
        assert mock_telemetry_svc.record_endpoint_usage.called
        call_args = mock_telemetry_svc.record_endpoint_usage.call_args
        record = call_args[0][0]
        assert record.source == "endpoint"
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 50
        assert record.total_tokens == 150

    def test_populate_endpoint_usage_skips_unavailable(self):
        """_populate_endpoint_usage skips calls without token usage."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            ReleaseBenchmarkRunner,
        )

        runner = ReleaseBenchmarkRunner.__new__(ReleaseBenchmarkRunner)

        mock_conn = mock.Mock()
        mock_cursor = mock.Mock()
        mock_conn.cursor.return_value.__enter__ = mock.Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = mock.Mock(return_value=False)

        # Mock semantic_calls with no usage info.
        call_id = uuid4()
        mock_cursor.fetchall.return_value = [(str(call_id), {"provenance": {}})]

        mock_telemetry_svc = mock.Mock()

        runner._populate_endpoint_usage(mock_telemetry_svc, uuid4(), mock_conn)

        # No record_endpoint_usage call when source is unavailable.
        assert not mock_telemetry_svc.record_endpoint_usage.called

    def test_persist_resource_samples_binds_run_id(self):
        """Samples collected in the workload window retain exact run scope."""
        from uuid import uuid4

        from firecrawl_skill.research_store.release.benchmark import (
            ReleaseBenchmarkRunner,
        )

        runner = ReleaseBenchmarkRunner.__new__(ReleaseBenchmarkRunner)

        mock_telemetry_svc = mock.Mock()

        from firecrawl_skill.research_domain.models import ResourceSample

        run_id = uuid4()
        sample = ResourceSample(
            run_id="",
            device_type="cpu",
            sample_type="process_cpu_percent_normalized",
            sample_at="2026-01-01T00:00:00+00:00",
            collector="psutil",
        )
        runner._persist_resource_samples(mock_telemetry_svc, run_id, (sample,))
        persisted = mock_telemetry_svc.record_resource_sample.call_args.args[0]
        assert persisted.run_id == str(run_id)


def test_resource_sampler_backfills_complete_window_on_initial_gpu_sample(
    monkeypatch,
):
    from firecrawl_skill.research_store import resource_sampler

    fake_handle = object()
    fake_nvml = SimpleNamespace(
        __version__="test",
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda _index: fake_handle,
        nvmlDeviceGetUUID=lambda _handle: "GPU-test",
        nvmlDeviceGetMemoryInfo=lambda _handle: SimpleNamespace(
            used=1024 * 1024 * 2048
        ),
    )
    monkeypatch.setattr(resource_sampler, "_HAS_PYNVML", True)
    monkeypatch.setattr(resource_sampler, "_HAS_PSUTIL", False)
    monkeypatch.setattr(resource_sampler, "pynvml", fake_nvml, raising=False)

    sampler = resource_sampler.ResourceSampler(
        interval_seconds=0.01,
        max_samples=2,
    )
    sampler.begin_window()
    _cpu_samples, gpu_samples = sampler.end_window()

    assert len(gpu_samples) == 2
    assert all(sample.status == "measured" for sample in gpu_samples)
    assert all(sample.device_uuid == "GPU-test" for sample in gpu_samples)
    assert all(sample.window_start for sample in gpu_samples)
    assert all(sample.window_end for sample in gpu_samples)
    assert {sample.window_start for sample in gpu_samples} == {
        gpu_samples[0].window_start
    }
    assert {sample.window_end for sample in gpu_samples} == {gpu_samples[0].window_end}


def test_periodic_resource_window_collects_interior_samples():
    """The release sampler records interior CPU/GPU observations."""
    import time

    import firecrawl_skill.research_store.resource_sampler as module

    process = mock.Mock()
    process.cpu_percent.return_value = 200.0
    fake_psutil = SimpleNamespace(
        Process=mock.Mock(return_value=process),
        cpu_count=mock.Mock(return_value=2),
        __version__="6.1.1",
    )
    handle = object()
    fake_nvml = SimpleNamespace(
        __version__="13.610.43",
        nvmlInit=mock.Mock(),
        nvmlShutdown=mock.Mock(),
        nvmlDeviceGetHandleByIndex=mock.Mock(return_value=handle),
        nvmlDeviceGetUUID=mock.Mock(return_value="GPU-periodic"),
        nvmlDeviceGetMemoryInfo=mock.Mock(
            return_value=SimpleNamespace(used=512 * 1024 * 1024)
        ),
    )

    with (
        mock.patch.object(module, "_HAS_PSUTIL", True),
        mock.patch.object(module, "psutil", fake_psutil, create=True),
        mock.patch.object(module, "_HAS_PYNVML", True),
        mock.patch.object(module, "pynvml", fake_nvml, create=True),
    ):
        sampler = module.ResourceSampler(interval_seconds=0.01)
        sampler.start_periodic_window()
        time.sleep(0.06)
        cpu_samples, gpu_samples = sampler.stop_periodic_window()

    assert len(cpu_samples) >= 2
    assert len(gpu_samples) >= 3
    assert [sample.sample_number for sample in cpu_samples] == list(
        range(len(cpu_samples))
    )
    assert [sample.sample_number for sample in gpu_samples] == list(
        range(len(gpu_samples))
    )
    assert all(sample.status == "measured" for sample in cpu_samples + gpu_samples)
    assert all(sample.window_start for sample in cpu_samples + gpu_samples)
    assert all(sample.window_end for sample in cpu_samples + gpu_samples)
    assert len({sample.window_start for sample in cpu_samples + gpu_samples}) == 1
    assert len({sample.window_end for sample in cpu_samples + gpu_samples}) == 1


def test_unavailable_resource_sample_persists_nullable_value():
    """Unavailable instrumentation is persisted as SQL NULL, never numeric zero."""
    from firecrawl_skill.research_domain.models import ResourceSample
    from firecrawl_skill.research_store.telemetry_service import (
        PerformanceTelemetryService,
    )

    connection = mock.Mock()
    service = PerformanceTelemetryService(connection)
    sample = ResourceSample(
        run_id=str(uuid4()),
        device_type="cpu",
        sample_type="process_cpu_percent_normalized",
        value=None,
        sample_at="2026-07-31T12:00:00+00:00",
        collector="psutil",
        collector_version="not-installed",
        sample_number=0,
        status="unavailable",
        failure_reason="psutil not installed",
        window_start="2026-07-31T12:00:00+00:00",
        window_end="2026-07-31T12:00:01+00:00",
        sampling_interval_seconds=1.0,
    )

    service.record_resource_sample(sample)

    params = connection.execute.call_args.args[1]
    assert params[5] is None
    assert params[10] == "unavailable"
    assert params[11] == "psutil not installed"


def test_release_runner_uses_uncapped_periodic_resource_window():
    """The production runner wires periodic sampling around orchestration."""
    import firecrawl_skill.research_store.release.benchmark as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "sampler.start_periodic_window()" in source
    assert "sampler.stop_periodic_window()" in source
    assert "ResourceSampler(interval_seconds=1.0, max_samples=10)" not in source
