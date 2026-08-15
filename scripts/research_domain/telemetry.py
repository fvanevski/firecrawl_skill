"""Run-scoped performance telemetry domain contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TelemetryStatus(str, Enum):
    """Status vocabulary for telemetry availability.

    Measured zero (a real instrument returned 0) is distinct from
    unavailable (the instrument is absent or failed).
    """

    MEASURED = "measured"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    STALE = "stale"
    INVALID = "invalid"


class CacheEventType(str, Enum):
    """Types of cache events recorded for a benchmark run."""

    LOOKUP = "lookup"
    HIT = "hit"
    MISS = "miss"
    INVALIDATION = "invalidation"
    REUSE = "reuse"


class EndpointType(str, Enum):
    """Types of model endpoints tracked for telemetry."""

    GENERATIVE = "generative"
    EMBEDDING = "embedding"
    RERANKING = "reranking"


@dataclass(frozen=True)
class TokenAccounting:
    """Token counts for a semantic call, measured or tokenizer-derived.

    Attributes:
        schema_version: One of ``"token-accounting-v1"``.
        prompt_tokens: Prompt token count from endpoint response (None if
            unavailable).
        completion_tokens: Completion token count from endpoint response
            (None if unavailable).
        total_tokens: Total token count from endpoint response (None if
            unavailable). May differ from prompt+completion when the
            endpoint reports a separate total.
        tokenizer_prompt_tokens: Token count derived via tokenizer when
            endpoint usage is absent.
        tokenizer_completion_tokens: Token count derived via tokenizer when
            endpoint usage is absent.
        tokenizer_total_tokens: Total derived from tokenizer.
        source: ``"endpoint"`` when endpoint response provides usage,
            ``"tokenizer"`` when derived from the stored request/response,
            ``"unavailable"`` when neither is available.
        metric_version: Version of the token-accounting method.
    """

    schema_version: str = "token-accounting-v1"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokenizer_prompt_tokens: int | None = None
    tokenizer_completion_tokens: int | None = None
    tokenizer_total_tokens: int | None = None
    source: str = "unavailable"
    metric_version: str = "token-accounting-v1"

    SCHEMA_VERSION = "token-accounting-v1"
    SCHEMA_VERSIONS = ("token-accounting-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "token-accounting-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.source}"
            )

        # If endpoint source, at least one field must be present.
        if self.source == "endpoint" and (
            self.prompt_tokens is None
            and self.completion_tokens is None
            and self.total_tokens is None
        ):
            raise ValueError(
                "endpoint source requires at least one of prompt_tokens, "
                "completion_tokens, or total_tokens"
            )

        # If tokenizer source, at least one field must be present.
        if self.source == "tokenizer" and (
            self.tokenizer_prompt_tokens is None
            and self.tokenizer_completion_tokens is None
            and self.tokenizer_total_tokens is None
        ):
            raise ValueError(
                "tokenizer source requires at least one of tokenizer_prompt_tokens, "
                "tokenizer_completion_tokens, or tokenizer_total_tokens"
            )

    @property
    def total(self) -> int | None:
        """Return the total token count from the preferred source."""
        if self.source == "endpoint" and self.total_tokens is not None:
            return self.total_tokens
        if self.source == "tokenizer" and self.tokenizer_total_tokens is not None:
            return self.tokenizer_total_tokens
        if (
            self.source == "endpoint"
            and self.prompt_tokens is not None
            and self.completion_tokens is not None
        ):
            return self.prompt_tokens + self.completion_tokens
        if (
            self.source == "tokenizer"
            and self.tokenizer_prompt_tokens is not None
            and self.tokenizer_completion_tokens is not None
        ):
            return self.tokenizer_prompt_tokens + self.tokenizer_completion_tokens
        return None

    @property
    def status(self) -> TelemetryStatus:
        """Return the availability status of this token accounting."""
        if self.source == "endpoint":
            return TelemetryStatus.MEASURED
        if self.source == "tokenizer":
            return TelemetryStatus.MEASURED
        return TelemetryStatus.UNAVAILABLE


@dataclass(frozen=True)
class CacheEvent:
    """A single cache event scoped to a run and semantic stage.

    Attributes:
        schema_version: Always ``"cache-event-v1"``.
        run_id: UUID of the research run.
        stage: Semantic stage (outline, binding, draft, citation_pass).
        event_type: Type of cache event.
        key_hash: SHA-256 key hash of the cache entry.
        model_fingerprint: Model fingerprint for the cache entry.
        hit: Whether this lookup resulted in a cache hit (for LOOKUP events).
        metric_version: Version of the cache-event schema.
    """

    schema_version: str = "cache-event-v1"
    run_id: str = ""
    stage: str = ""
    event_type: str = ""
    key_hash: str = ""
    model_fingerprint: str = ""
    hit: bool | None = None
    metric_version: str = "cache-event-v1"

    SCHEMA_VERSION = "cache-event-v1"
    SCHEMA_VERSIONS = ("cache-event-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "cache-event-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.event_type not in ("lookup", "hit", "miss", "invalidation", "reuse"):
            raise ValueError(
                f"event_type must be lookup, hit, miss, invalidation, or reuse; got: {self.event_type}"
            )
        if self.event_type in ("hit", "miss", "lookup") and self.hit is None:
            raise ValueError(f"hit field is required for event_type={self.event_type}")


@dataclass(frozen=True)
class EmbeddingThroughputRecord:
    """Record of embedding throughput for a run or stage.

    Attributes:
        schema_version: Always ``"embedding-throughput-v1"``.
        run_id: UUID of the research run.
        stage: Stage name (e.g. "embedding", "indexing").
        batch_count: Number of batch requests made.
        vector_count: Number of vectors produced (excluding failures).
        failed_count: Number of embedding requests that failed.
        total_texts: Total input texts processed.
        elapsed_seconds: Wall-clock time spent in embedding calls.
        endpoint_url: The embedding endpoint URL.
        endpoint_model: The model name used.
        dimension: Vector dimension produced.
        metric_version: Version of the embedding-throughput schema.
    """

    schema_version: str = "embedding-throughput-v1"
    run_id: str = ""
    stage: str = ""
    batch_count: int = 0
    vector_count: int = 0
    failed_count: int = 0
    total_texts: int = 0
    elapsed_seconds: float = 0.0
    endpoint_url: str = ""
    endpoint_model: str = ""
    dimension: int | None = None
    metric_version: str = "embedding-throughput-v1"

    SCHEMA_VERSION = "embedding-throughput-v1"
    SCHEMA_VERSIONS = ("embedding-throughput-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "embedding-throughput-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")

    @property
    def throughput(self) -> float:
        """Texts per second, or 0.0 when no elapsed time."""
        if self.elapsed_seconds <= 0:
            return 0.0
        return round(self.total_texts / self.elapsed_seconds, 3)

    @property
    def status(self) -> TelemetryStatus:
        """Return availability status."""
        if self.batch_count > 0 and self.elapsed_seconds > 0:
            return TelemetryStatus.MEASURED
        if self.batch_count == 0 and self.elapsed_seconds == 0:
            return TelemetryStatus.UNAVAILABLE
        return TelemetryStatus.PARTIAL


@dataclass(frozen=True)
class ResourceSample:
    """A single CPU or GPU resource sample over the run window.

    Attributes:
        schema_version: Always ``"resource-sample-v1"``.
        run_id: UUID of the research run.
        device_type: ``"cpu"`` or ``"gpu"``.
        device_index: Hardware device index (0-based).
        device_uuid: Hardware UUID, when available.
        sample_type: Type of measurement (e.g. ``"cpu_percent"``, ``"gpu_memory_used_mb"``).
        value: The measured value. Nullable — samples with ``status != 'measured'``
            may have ``value=None``.
        sample_at: ISO-8601 timestamp of the sample.
        collector: Library used (e.g. ``"psutil"``, ``"pynvml"``).
        collector_version: Version of the collector library.
        sample_number: Sequential sample number within the run.
        metric_version: Version of the resource-sample schema.
        status: Availability status of this sample.
        failure_reason: Explicit reason when status is not ``"measured"``.
        window_start: ISO-8601 timestamp when the workload window began.
        window_end: ISO-8601 timestamp when the workload window ended.
        sampling_interval_seconds: Interval between samples in the workload window.
    """

    schema_version: str = "resource-sample-v1"
    run_id: str = ""
    device_type: str = ""
    device_index: int = 0
    device_uuid: str = ""
    sample_type: str = ""
    value: float | None = None
    sample_at: str = ""
    collector: str = ""
    collector_version: str = ""
    sample_number: int = 0
    metric_version: str = "resource-sample-v1"
    status: str = "measured"
    failure_reason: str = ""
    window_start: str = ""
    window_end: str = ""
    sampling_interval_seconds: float = 0.0

    SCHEMA_VERSION = "resource-sample-v1"
    SCHEMA_VERSIONS = ("resource-sample-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "resource-sample-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.device_type not in ("cpu", "gpu"):
            raise ValueError(f"device_type must be cpu or gpu; got: {self.device_type}")
        if self.status not in (
            "measured",
            "unavailable",
            "partial",
            "stale",
            "invalid",
        ):
            raise ValueError(f"invalid status: {self.status}")


@dataclass(frozen=True)
class EndpointUsageRecord:
    """Record of endpoint usage for a semantic call.

    Captures actual token usage from the endpoint response, or falls back
    to tokenizer-based counting when the endpoint does not provide usage.

    Attributes:
        schema_version: Always ``"endpoint-usage-v1"``.
        run_id: UUID of the research run.
        call_id: UUID of the semantic call.
        endpoint_type: Type of endpoint (generative, embedding, reranking).
        provider: Provider name (e.g. ``"openai-compatible"``).
        model: Model name.
        model_revision: Model revision string.
        prompt_tokens: From endpoint response or tokenizer.
        completion_tokens: From endpoint response or tokenizer.
        total_tokens: From endpoint response or tokenizer.
        source: ``"endpoint"`` or ``"tokenizer"``.
        metric_version: Version of the endpoint-usage schema.
    """

    schema_version: str = "endpoint-usage-v1"
    run_id: str = ""
    call_id: str = ""
    endpoint_type: str = ""
    provider: str = ""
    model: str = ""
    model_revision: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    source: str = "unavailable"
    metric_version: str = "endpoint-usage-v1"

    SCHEMA_VERSION = "endpoint-usage-v1"
    SCHEMA_VERSIONS = ("endpoint-usage-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "endpoint-usage-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.source}"
            )
        if self.endpoint_type and self.endpoint_type not in (
            "generative",
            "embedding",
            "reranking",
        ):
            raise ValueError(f"invalid endpoint_type: {self.endpoint_type}")


@dataclass(frozen=True)
class PerformanceTelemetrySummary:
    """Aggregated run-scoped performance telemetry summary.

    Attributes:
        schema_version: Always ``"performance-telemetry-summary-v1"``.
        run_id: UUID of the research run.
        total_tokens: Sum of all token counts for the run.
        token_source: Source of the token counts
            (``"endpoint"``, ``"tokenizer"``, ``"unavailable"``).
        semantic_calls: Total semantic calls for the run.
        cache_lookups: Total cache lookups for the run.
        cache_hits: Total cache hits for the run.
        cache_misses: Total cache misses for the run.
        cache_hit_rate: Cache hit rate (0.0–1.0), or None when unavailable.
        embedding_batch_count: Total embedding batches.
        embedding_vector_count: Total embedding vectors produced.
        embedding_elapsed_seconds: Total embedding time.
        embedding_throughput: Texts per second, or 0.0 when unavailable.
        cpu_samples: Number of CPU samples collected.
        cpu_mean_percent: Mean CPU usage, or None when no samples.
        cpu_max_percent: Maximum CPU usage, or None when no samples.
        gpu_samples: Number of GPU samples collected.
        gpu_mean_memory_mb: Mean GPU memory, or None when no samples.
        gpu_max_memory_mb: Maximum GPU memory, or None when no samples.
        gpu_unavailable: Whether GPU telemetry is unavailable.
            GPU is optional — unavailability does not cause strict_pass
            to be False. This accommodates CPU-only environments where
            NVML is absent or the GPU is reserved for the local LLM agent.
        strict_pass: Whether all required metrics are measured (not estimated).
            GPU unavailability does not affect strict_pass.
        metric_version: Version of the summary schema.
    """

    schema_version: str = "performance-telemetry-summary-v1"
    run_id: str = ""
    total_tokens: int = 0
    token_source: str = "unavailable"
    semantic_calls: int = 0
    cache_lookups: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_rate: float | None = None
    embedding_batch_count: int = 0
    embedding_vector_count: int = 0
    embedding_elapsed_seconds: float = 0.0
    embedding_throughput: float = 0.0
    cpu_samples: int = 0
    cpu_mean_percent: float | None = None
    cpu_max_percent: float | None = None
    gpu_samples: int = 0
    gpu_mean_memory_mb: float | None = None
    gpu_max_memory_mb: float | None = None
    gpu_unavailable: bool = True
    strict_pass: bool = True
    metric_version: str = "performance-telemetry-summary-v1"

    SCHEMA_VERSION = "performance-telemetry-summary-v1"
    SCHEMA_VERSIONS = ("performance-telemetry-summary-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "performance-telemetry-summary-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.token_source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "token_source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.token_source}"
            )
        if self.cache_hit_rate is not None and not (0.0 <= self.cache_hit_rate <= 1.0):
            raise ValueError("cache_hit_rate must be between 0.0 and 1.0")
