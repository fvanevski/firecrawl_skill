from __future__ import annotations

import hashlib
from pathlib import Path

path = Path("scripts/_apply_pr157_thread_fixes.py")
text = path.read_text(encoding="utf-8")
expected_input = "1b0c3e85b97230ada1401ebc680d3212c74cdc4770c4904bb5ec2d89f712d63f"
actual_input = hashlib.sha256(text.encode()).hexdigest()
if actual_input != expected_input:
    raise SystemExit(f"unexpected reconstructed driver SHA: {actual_input}")

old = '''STRICT_ACCEPTABLE_PERFORMANCE_STATUSES = frozenset(
    {MetricStatus.MEASURED, MetricStatus.NOT_APPLICABLE}
)
STRICT_MIN_CPU_SAMPLES = 2
'''
new = '''STRICT_ACCEPTABLE_QUALITY_STATUSES = frozenset({MetricStatus.MEASURED})
STRICT_ACCEPTABLE_PERFORMANCE_STATUSES = frozenset(
    {MetricStatus.MEASURED, MetricStatus.NOT_APPLICABLE}
)
STRICT_MIN_CPU_SAMPLES = 2
'''
if text.count(old) != 1:
    raise SystemExit("acceptable-status constant target mismatch")
text = text.replace(old, new, 1)

old = '''    replace_once(
        "scripts/research_store/release_benchmark.py",
        \'\'\'                        if status not in (
                            MetricStatus.MEASURED,
                            MetricStatus.NOT_APPLICABLE,
                        ):
\'\'\',
        \'\'\'                        if status not in STRICT_ACCEPTABLE_PERFORMANCE_STATUSES:
\'\'\',
    )
'''
new = '''    replace_once(
        "scripts/research_store/release_benchmark.py",
        \'\'\'                    # Reject if any mandatory quality metric is missing or not MEASURED (excluding NOT_APPLICABLE)
                    observed_quality = {
                        qm.name: qm.status for qm in result.quality_metrics
                    }
                    for metric in MANDATORY_QUALITY_METRICS:
                        status = observed_quality.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in (
                            MetricStatus.MEASURED,
                            MetricStatus.NOT_APPLICABLE,
                        ):
\'\'\',
        \'\'\'                    # Quality evidence must be affirmatively measured.
                    observed_quality = {
                        qm.name: qm.status for qm in result.quality_metrics
                    }
                    for metric in MANDATORY_QUALITY_METRICS:
                        status = observed_quality.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in STRICT_ACCEPTABLE_QUALITY_STATUSES:
\'\'\',
    )
    replace_once(
        "scripts/research_store/release_benchmark.py",
        \'\'\'                    # Reject if any mandatory performance metric is missing or not MEASURED (excluding NOT_APPLICABLE)
                    observed_perf = {
                        pm.name: pm.status for pm in result.performance_metrics
                    }
                    for metric in MANDATORY_PERFORMANCE_METRICS:
                        status = observed_perf.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in (
                            MetricStatus.MEASURED,
                            MetricStatus.NOT_APPLICABLE,
                        ):
\'\'\',
        \'\'\'                    # Performance evidence may be explicitly mode-scoped N/A.
                    observed_perf = {
                        pm.name: pm.status for pm in result.performance_metrics
                    }
                    for metric in MANDATORY_PERFORMANCE_METRICS:
                        status = observed_perf.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in STRICT_ACCEPTABLE_PERFORMANCE_STATUSES:
\'\'\',
    )
'''
if text.count(old) != 1:
    raise SystemExit("strict recommendation replacement target mismatch")
text = text.replace(old, new, 1)

marker = '''    append_once(
        "scripts/test_release_invariant_contracts.py",
        "def test_strict_resource_metrics_require_periodic_window",
'''
insert = '''    replace_once(
        "scripts/test_release_invariant_contracts.py",
        \'\'\'def _recommend(
    *,
    mode: str,
    performance: PerformanceMeasurement,
    performance_metrics: tuple[PerformanceMetric, ...],
    errors: tuple[str, ...] = (),
) -> ReleaseRecommendation:
\'\'\',
        \'\'\'def _recommend(
    *,
    mode: str,
    performance: PerformanceMeasurement,
    performance_metrics: tuple[PerformanceMetric, ...],
    quality_metrics: tuple[QualityMetric, ...] | None = None,
    errors: tuple[str, ...] = (),
) -> ReleaseRecommendation:
\'\'\',
    )
    replace_once(
        "scripts/test_release_invariant_contracts.py",
        \'\'\'        quality_metrics=_quality_metrics(run_id),
        performance_metrics=performance_metrics,
\'\'\',
        \'\'\'        quality_metrics=quality_metrics or _quality_metrics(run_id),
        performance_metrics=performance_metrics,
\'\'\',
    )

'''
if text.count(marker) != 1:
    raise SystemExit("test helper insertion target mismatch")
text = text.replace(marker, insert + marker, 1)

old = '''    assert any(
        "performance metric cpu_percent is unavailable" in claim
        for claim in recommendation.withdrawn_claims
    )
\'\'\',
    )
'''
new = '''    assert any(
        "performance metric cpu_percent is unavailable" in claim
        for claim in recommendation.withdrawn_claims
    )


def test_strict_policy_rejects_not_applicable_quality_metric():
    """Quality evidence cannot satisfy strict policy through N/A status."""
    run_id = uuid4()
    quality_metrics = list(_quality_metrics(run_id))
    target_index = next(
        index
        for index, metric in enumerate(quality_metrics)
        if metric.name == "candidate_recall"
    )
    original = quality_metrics[target_index]
    quality_metrics[target_index] = QualityMetric(
        name=original.name,
        value=None,
        source=original.source,
        formula="not applicable regression fixture",
        status=MetricStatus.NOT_APPLICABLE,
    )

    recommendation = _recommend(
        mode="autonomous_local",
        performance=_performance(),
        performance_metrics=_performance_metrics(uuid4()),
        quality_metrics=tuple(quality_metrics),
    )

    assert recommendation.outcome.value == "no_go"
    assert any(
        "quality metric candidate_recall is not_applicable" in claim
        for claim in recommendation.withdrawn_claims
    )
\'\'\',
    )
'''
if text.count(old) != 1:
    raise SystemExit("quality N/A regression insertion target mismatch")
text = text.replace(old, new, 1)

expected_output = "5c3c892283dff7c67f34f1daf2a22ed41495afbdf5440ffb5f0a90bff9c3e819"
actual_output = hashlib.sha256(text.encode()).hexdigest()
if actual_output != expected_output:
    raise SystemExit(f"adjusted driver SHA mismatch: {actual_output}")
compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")
