"""CPU and GPU resource sampler for run-scoped performance telemetry.

This module provides:

* ``ResourceSampler`` — collects CPU and GPU samples over a run window.
* ``ResourceSummary`` — aggregated summary of samples (mean, max, count).
* Explicit availability state: missing psutil/NVML is ``unavailable``,
  not zero.

## Authoritative state

- Samples are persisted in the ``run_resource_samples`` table.
- A summary row is written to ``run_performance_telemetry`` after
  sampling completes.
- Missing instrumentation (no psutil, no NVML) is recorded as
  ``status = 'unavailable'``, never as numeric zero.

## Invariants

1. CPU sampling uses ``psutil.Process().cpu_percent()`` across the exact
   workload window and normalizes by logical CPU count. When psutil is absent, CPU is
   marked ``unavailable``.
2. GPU sampling uses ``pynvml`` to query ``nvmlDeviceGetMemoryInfo``.
   An absent library is ``unavailable``; an installed collector or driver
   failure is ``invalid``.
3. Samples are collected at a fixed interval over the run window.
4. The sampler records device index, UUID, collector library, and
   collector version for provenance.
"""

from __future__ import annotations

import datetime
import importlib.metadata
import logging
import time
from dataclasses import dataclass
from typing import Any

try:
    import psutil  # type: ignore[import-not-found,import-untyped]

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

try:
    import pynvml  # type: ignore[import-not-found,import-untyped]

    _HAS_PYNVML = True
except ImportError:
    _HAS_PYNVML = False

from research_domain.models import ResourceSample

logger = logging.getLogger(__name__)


def _collector_version(module: Any, distribution: str) -> str:
    """Return an explicit collector version, including namespace packages."""
    module_version = getattr(module, "__version__", "")
    if module_version:
        return str(module_version)
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class ResourceSummary:
    """Aggregated summary of resource samples.

    Attributes:
        device_type: ``"cpu"`` or ``"gpu"``.
        sample_count: Number of samples collected.
        mean: Mean value, or ``None`` when no samples.
        maximum: Maximum value, or ``None`` when no samples.
        status: ``"measured"``, ``"unavailable"``, or ``"partial"``.
        device_index: Hardware device index.
        device_uuid: Hardware UUID, when available.
        collector: Collector library name.
        sample_interval_seconds: Interval between samples.
    """

    device_type: str
    sample_count: int = 0
    mean: float | None = None
    maximum: float | None = None
    status: str = "unavailable"
    device_index: int = 0
    device_uuid: str = ""
    collector: str = ""
    sample_interval_seconds: float = 0.0


class ResourceSampler:
    """Collect CPU and GPU samples over a run window.

    Args:
        interval_seconds: Seconds between samples.
        cpu_device_index: CPU device index (always 0 for psutil).
        gpu_device_index: GPU device index for NVML (default 0).
        max_samples: Maximum number of samples to collect per device.
            ``0`` means no limit.
    """

    def __init__(
        self,
        interval_seconds: float = 1.0,
        cpu_device_index: int = 0,
        gpu_device_index: int = 0,
        max_samples: int = 0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.cpu_device_index = cpu_device_index
        self.gpu_device_index = gpu_device_index
        self.max_samples = max_samples

        # Collected samples per device type.
        self._cpu_samples: list[ResourceSample] = []
        self._gpu_samples: list[ResourceSample] = []
        self._cpu_sample_number = 0
        self._gpu_sample_number = 0

        # NVML handle (lazy init).
        self._nvml_handle: Any = None
        self._nvml_initialized = False
        self._gpu_unavailable = True
        self._device_uuid = ""
        self._window_started_at: float | None = None
        self._cpu_process: Any = psutil.Process() if _HAS_PSUTIL else None

    @property
    def cpu_available(self) -> bool:
        """Return True if psutil is available for CPU sampling."""
        return _HAS_PSUTIL

    @property
    def gpu_available(self) -> bool:
        """Return True when the NVML Python collector is installed."""
        return _HAS_PYNVML

    def begin_window(self) -> None:
        """Establish collector baselines immediately before the workload."""
        self._window_started_at = time.monotonic()
        if self._cpu_process is not None:
            # Process.cpu_percent() is a delta measurement. The first
            # non-blocking result is meaningless and must be discarded.
            self._cpu_process.cpu_percent(interval=None)
        self.collect_gpu_sample()

    def end_window(self) -> tuple[list[ResourceSample], list[ResourceSample]]:
        """Collect final samples immediately after the workload."""
        if self._window_started_at is None:
            raise RuntimeError("begin_window() must be called before end_window()")
        self.collect_cpu_sample()
        self.collect_gpu_sample()
        self._window_started_at = None
        if self._nvml_initialized:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                logger.warning("NVML shutdown failed", exc_info=True)
            finally:
                self._nvml_initialized = False
                self._nvml_handle = None
        return self._cpu_samples[:], self._gpu_samples[:]

    def _init_nvml(self) -> None:
        """Initialize NVML lazily on first GPU sample."""
        if self._nvml_initialized:
            return
        if not _HAS_PYNVML:
            self._gpu_unavailable = True
            return
        try:
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(self.gpu_device_index)
            self._nvml_handle = handle
            self._nvml_initialized = True
            self._gpu_unavailable = False
            # Record device UUID.
            try:
                uuid_str = pynvml.nvmlDeviceGetUUID(handle)
                self._device_uuid = uuid_str
            except Exception:  # noqa: BLE001
                self._device_uuid = ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("NVML init failed: %s", exc)
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: S110, BLE001
                pass
            self._nvml_initialized = False
            self._gpu_unavailable = True

    def collect_cpu_sample(self, sample_at: str | None = None) -> ResourceSample | None:
        """Collect a single CPU sample.

        Args:
            sample_at: ISO-8601 timestamp. Defaults to current UTC time.

        Returns:
            A ResourceSample, or ``None`` if psutil is unavailable.
        """
        if self.max_samples and self._cpu_sample_number >= self.max_samples:
            return None

        if not _HAS_PSUTIL:
            sample = ResourceSample(
                run_id="",
                device_type="cpu",
                device_index=self.cpu_device_index,
                device_uuid="",
                sample_type="process_cpu_percent_normalized",
                value=0.0,
                sample_at=sample_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                collector="psutil",
                collector_version="not-installed",
                sample_number=self._cpu_sample_number,
                status="unavailable",
            )
            self._cpu_samples.append(sample)
            self._cpu_sample_number += 1
            return sample

        try:
            if self._window_started_at is None:
                raise RuntimeError("CPU sample is not bound to a workload window")
            logical_cpus = psutil.cpu_count() or 1
            value = self._cpu_process.cpu_percent(interval=None) / logical_cpus
        except Exception as exc:  # noqa: BLE001
            logger.warning("CPU sample failed: %s", exc)
            sample = ResourceSample(
                run_id="",
                device_type="cpu",
                device_index=self.cpu_device_index,
                device_uuid="",
                sample_type="process_cpu_percent_normalized",
                value=0.0,
                sample_at=sample_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                collector="psutil",
                collector_version=_collector_version(psutil, "psutil"),
                sample_number=self._cpu_sample_number,
                status="invalid",
            )
            self._cpu_samples.append(sample)
            self._cpu_sample_number += 1
            return sample

        sample = ResourceSample(
            run_id="",
            device_type="cpu",
            device_index=self.cpu_device_index,
            device_uuid="",
            sample_type="process_cpu_percent_normalized",
            value=round(value, 2),
            sample_at=sample_at
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            collector="psutil",
            collector_version=_collector_version(psutil, "psutil"),
            sample_number=self._cpu_sample_number,
            status="measured",
        )
        self._cpu_samples.append(sample)
        self._cpu_sample_number += 1
        return sample

    def collect_gpu_sample(self, sample_at: str | None = None) -> ResourceSample | None:
        """Collect a single GPU memory sample.

        Args:
            sample_at: ISO-8601 timestamp. Defaults to current UTC time.

        Returns:
            A ResourceSample, or ``None`` if NVML is unavailable.
        """
        if self.max_samples and self._gpu_sample_number >= self.max_samples:
            return None

        self._init_nvml()
        if self._gpu_unavailable:
            sample = ResourceSample(
                run_id="",
                device_type="gpu",
                device_index=self.gpu_device_index,
                device_uuid=self._device_uuid,
                sample_type="gpu_memory_used_mb",
                value=0.0,
                sample_at=sample_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                collector="pynvml",
                collector_version=(
                    _collector_version(pynvml, "nvidia-ml-py")
                    if _HAS_PYNVML
                    else "not-installed"
                ),
                sample_number=self._gpu_sample_number,
                status="invalid" if _HAS_PYNVML else "unavailable",
            )
            self._gpu_samples.append(sample)
            self._gpu_sample_number += 1
            return sample

        try:
            handle = self._nvml_handle
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            value = round(info.used / (1024 * 1024), 2)  # MB
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPU sample failed: %s", exc)
            sample = ResourceSample(
                run_id="",
                device_type="gpu",
                device_index=self.gpu_device_index,
                device_uuid=getattr(self, "_device_uuid", ""),
                sample_type="gpu_memory_used_mb",
                value=0.0,
                sample_at=sample_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                collector="pynvml",
                collector_version=_collector_version(pynvml, "nvidia-ml-py"),
                sample_number=self._gpu_sample_number,
                status="invalid",
            )
            self._gpu_samples.append(sample)
            self._gpu_sample_number += 1
            return sample

        sample = ResourceSample(
            run_id="",
            device_type="gpu",
            device_index=self.gpu_device_index,
            device_uuid=getattr(self, "_device_uuid", ""),
            sample_type="gpu_memory_used_mb",
            value=value,
            sample_at=sample_at
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            collector="pynvml",
            collector_version=_collector_version(pynvml, "nvidia-ml-py"),
            sample_number=self._gpu_sample_number,
            status="measured",
        )
        self._gpu_samples.append(sample)
        self._gpu_sample_number += 1
        return sample

    def collect(self) -> tuple[list[ResourceSample], list[ResourceSample]]:
        """Collect one CPU and one GPU sample.

        Returns:
            A tuple of ``(cpu_samples, gpu_samples)`` lists.
        """
        cpu = self.collect_cpu_sample()
        gpu = self.collect_gpu_sample()
        return (
            [cpu] if cpu else [],
            [gpu] if gpu else [],
        )

    def run_window(
        self, duration_seconds: float
    ) -> tuple[list[ResourceSample], list[ResourceSample]]:
        """Sample over a run window.

        Collects samples at ``interval_seconds`` intervals for approximately
        ``duration_seconds``.

        Args:
            duration_seconds: How long to sample.

        Returns:
            A tuple of ``(cpu_samples, gpu_samples)`` lists.
        """
        self.begin_window()
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration_seconds:
                break
            remaining = duration_seconds - elapsed
            sleep_time = min(self.interval_seconds, remaining)
            if sleep_time > 0:
                time.sleep(sleep_time)
            if time.monotonic() - start < duration_seconds:
                self.collect()

        return self.end_window()

    def summarize_cpu(self) -> ResourceSummary:
        """Summarize CPU samples.

        Returns:
            A ResourceSummary with mean, max, and count.
        """
        measured = [s for s in self._cpu_samples if s.status == "measured"]
        if not measured:
            return ResourceSummary(
                device_type="cpu",
                sample_count=len(self._cpu_samples),
                status="unavailable" if not _HAS_PSUTIL else "partial",
                collector="psutil",
            )
        values = [s.value for s in measured]
        return ResourceSummary(
            device_type="cpu",
            sample_count=len(measured),
            mean=round(sum(values) / len(values), 2),
            maximum=round(max(values), 2),
            status="measured",
            collector="psutil",
        )

    def summarize_gpu(self) -> ResourceSummary:
        """Summarize GPU samples.

        Returns:
            A ResourceSummary with mean, max, and count.
        """
        measured = [s for s in self._gpu_samples if s.status == "measured"]
        if not measured:
            return ResourceSummary(
                device_type="gpu",
                sample_count=len(self._gpu_samples),
                status="unavailable" if self._gpu_unavailable else "partial",
                collector="pynvml",
            )
        values = [s.value for s in measured]
        return ResourceSummary(
            device_type="gpu",
            sample_count=len(measured),
            mean=round(sum(values) / len(values), 2),
            maximum=round(max(values), 2),
            status="measured",
            collector="pynvml",
        )

    def shutdown(self) -> None:
        """Shutdown NVML if initialized."""
        if self._nvml_initialized and _HAS_PYNVML:
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: S110, BLE001
                pass
            self._nvml_initialized = False

    def reset(self) -> None:
        """Clear all collected samples."""
        self._cpu_samples.clear()
        self._gpu_samples.clear()
        self._cpu_sample_number = 0
        self._gpu_sample_number = 0
