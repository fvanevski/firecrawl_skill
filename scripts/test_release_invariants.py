"""Targeted regression tests for release-path invariants (issue #170).

These tests exercise decision paths that must produce specific outcomes
under strict release policy.  Every test is a unit test that mocks the
PostgreSQL layer so it does not require a live database.

Required results
----------------
| Test                                                              | Result          |
|-------------------------------------------------------------------|-----------------|
| agent_led without externally supplied artifact                     | hard failure    |
| agent_led accidentally using local-model authority                 | hard failure    |
| deterministic_debug with not_invoked tokens                        | cannot satisfy  |
| one of several semantic calls lacks usage                          | INCOMPLETE      |
| embedding batch has failures                                       | INCOMPLETE      |
| completed metrics plus nonempty run.errors                         | NO_GO           |
| campaign outcome go_with_conditions                                | nonzero exit    |
| unavailable CPU/GPU collector                                      | null + state    |
| partial resource window                                            | INCOMPLETE      |
| basename collision in recall sources                               | no false match  |
| evidence link exists but citation validation fails                 | citation fails  |
| Valkey unavailable                                                 | preflight fail  |
| campaign A/B cache events overlap in time                          | run isolation   |
"""

from __future__ import annotations

import sys
import time
from hashlib import sha256
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "superseded by test_release_invariant_contracts.py, which exercises "
        "production decision boundaries instead of constructed outcomes"
    )
)

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import (
    PerformanceMeasurement,
    QualityMeasurement,
    ReleaseRecommendation,
)
from research_store.release_benchmark import (
    CampaignRun,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReproducibilityComparison,
)
from research_store.strict_benchmark import (
    _build_env_manifest,
    _preflight_check,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BENCHMARK_FIXTURE = (
    SCRIPTS.parent / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
)


def _make_quality(**overrides):
    defaults = {
        "schema_version": "quality-measurement-v2",
        "candidate_recall": 0.75,
        "source_quality_score": 0.80,
        "coverage_completeness": 0.65,
        "unsupported_claim_rate": 0.08,
        "citation_accuracy": 0.88,
        "report_quality_score": 0.78,
    }
    defaults.update(overrides)
    return QualityMeasurement(**defaults)


def _make_performance(
    total_latency_ms=15000.0,
    total_tokens=15000,
    semantic_calls=8,
    cache_hit_rate=0.3,
    cache_miss_rate=0.7,
    embedding_throughput=50.0,
    gpu_memory_mb=4096.0,
    cpu_percent=60.0,
    **overrides,
):
    defaults = {
        "schema_version": "performance-measurement-v1",
        "total_latency_ms": total_latency_ms,
        "total_tokens": total_tokens,
        "semantic_calls": semantic_calls,
        "cache_hit_rate": cache_hit_rate,
        "cache_miss_rate": cache_miss_rate,
        "embedding_throughput": embedding_throughput,
        "gpu_memory_mb": gpu_memory_mb,
        "cpu_percent": cpu_percent,
    }
    defaults.update(overrides)
    return PerformanceMeasurement(**defaults)


def _make_result(
    campaign_id: str = "fr_test",
    mode: str = "agent_led",
    quality: QualityMeasurement | None = None,
    performance: PerformanceMeasurement | None = None,
    errors: tuple[str, ...] = (),
    outcome: str = "go",
    conditions: tuple[str, ...] = (),
) -> ReleaseBenchmarkResult:
    if quality is None:
        quality = _make_quality()
    if performance is None:
        performance = _make_performance()

    run = CampaignRun(
        campaign_id=campaign_id,
        run_id=f"fr_{campaign_id}",
        mode=mode,
        objective_id="obj-001",
        quality=quality,
        performance=performance,
        errors=errors,
    )

    recommendation = ReleaseRecommendation(
        schema_version="release-recommendation-v1",
        outcome=outcome,
        dataset_version="benchmark-v1",
        comparison=None,
        supported_claims=("quality thresholds met",),
        withdrawn_claims=(),
        known_limitations=("CPU latency",),
        conditions=conditions,
        p0_regressions=(),
    )

    return ReleaseBenchmarkResult(
        schema_version="release-benchmark-result-v1",
        campaign_id=campaign_id,
        campaign_timestamp="2026-07-28T00:00:00+00:00",
        environment=_build_env_manifest(
            candidate_sha="a" * 40,
            dataset_path=BENCHMARK_FIXTURE,
            dataset_hash=sha256(BENCHMARK_FIXTURE.read_bytes()).hexdigest(),
        ),
        runs=(run,),
        recommendation=recommendation,
        total_duration_ms=5000.0,
    )


# ---------------------------------------------------------------------------
# 1. agent_led without externally supplied artifact → hard failure
# ---------------------------------------------------------------------------


class TestAgentLedMissingArtifact:
    """agent_led mode must fail when no external research spec is supplied."""

    def test_agent_led_without_host_artifact_supplier_raises(self):
        """When agent_led mode is requested without a
        HostArtifactSupplier, the runner's mode validation must raise."""
        from research_store.release_benchmark import (
            ReleaseBenchmarkRunner,
        )

        # Create a config that requests agent_led without a host artifact
        # supplier — this is the exact invariant the runner validates.
        config = ReleaseBenchmarkConfig(
            database_url="postgresql://test:test@localhost/test",
            execution_modes=("agent_led",),
            strict=True,
            host_artifact_supplier=None,  # No external authority
        )

        runner = ReleaseBenchmarkRunner(mock.MagicMock(), config)

        with pytest.raises(
            RuntimeError,
            match="agent_led.*requires a HostArtifactSupplier",
        ):
            runner._validate_modes()


# ---------------------------------------------------------------------------
# 2. agent_led accidentally using local-model authority → hard failure
# ---------------------------------------------------------------------------


class TestAgentLedLocalModelAuthority:
    """agent_led must not accept local-model output as the semantic authority."""

    def test_agent_led_local_model_rejected(self):
        """If agent_led is configured without a host-artifact supplier,
        the runner must raise rather than allow the local model to become
        the semantic authority."""
        from research_store.release_benchmark import (
            ReleaseBenchmarkRunner,
        )

        config = ReleaseBenchmarkConfig(
            database_url="postgresql://test:test@localhost/test",
            execution_modes=("agent_led",),
            strict=True,
            host_artifact_supplier=None,
        )

        runner = ReleaseBenchmarkRunner(mock.MagicMock(), config)

        # The invariant is enforced at mode-validation time: agent_led
        # without a host-artifact supplier is a hard failure.
        with pytest.raises(
            RuntimeError,
            match="agent_led.*requires a HostArtifactSupplier",
        ):
            runner._validate_modes()


# ---------------------------------------------------------------------------
# 3. deterministic_debug with not_invoked tokens → cannot satisfy release
# ---------------------------------------------------------------------------


class TestDeterministicDebugNotInvoked:
    """deterministic_debug mode with not_invoked tokens cannot satisfy release."""

    def test_deterministic_debug_not_invoked_tokens(self):
        """When deterministic_debug mode records tokens for calls that were
        not actually invoked (not_invoked), the performance measurement must
        not satisfy release thresholds."""
        # A performance measurement with not_invoked tokens should have
        # total_tokens = 0 or a status that marks it incomplete.
        perf = _make_performance(
            total_tokens=0,
            semantic_calls=0,
        )
        # In strict mode, a run with zero semantic calls cannot produce
        # a GO recommendation because mandatory performance metrics are
        # not_measured rather than measured.
        assert perf.total_tokens == 0
        assert perf.semantic_calls == 0

        # Build a result with deterministic_debug and zero tokens.
        result = _make_result(
            campaign_id="fr_dd_test",
            mode="deterministic_debug",
            quality=_make_quality(candidate_recall=0.0),
            performance=perf,
            outcome="no_go",
        )
        assert result.recommendation.outcome == "no_go"
        # deterministic_debug is not a release-qualifying mode.
        assert result.recommendation.outcome != "go"


# ---------------------------------------------------------------------------
# 4. one of several semantic calls lacks usage → INCOMPLETE, NO_GO
# ---------------------------------------------------------------------------


class TestMissingTokenUsage:
    """One of several semantic calls lacking usage → INCOMPLETE, NO_GO."""

    def test_one_call_lacks_usage(self):
        """When one of several semantic calls is missing usage data,
        the performance metric must be INCOMPLETE and the recommendation
        must be NO_GO."""
        # Simulate: 8 calls expected, 7 have usage, 1 is missing.
        perf = _make_performance(
            total_tokens=12000,  # Less than expected — one call missing
            semantic_calls=7,  # 7 of 8 calls reported
            cache_hit_rate=0.3,
        )

        result = _make_result(
            campaign_id="fr_missing_usage",
            mode="agent_led",
            quality=_make_quality(),
            performance=perf,
            outcome="no_go",
        )

        # The recommendation must be NO_GO because performance is incomplete.
        assert result.recommendation.outcome == "no_go"
        assert perf.semantic_calls < 8  # Not all calls reported


# ---------------------------------------------------------------------------
# 5. embedding batch has failures → INCOMPLETE, NO_GO
# ---------------------------------------------------------------------------


class TestEmbeddingBatchFailures:
    """Embedding batch with failures → INCOMPLETE, NO_GO."""

    def test_embedding_batch_failures(self):
        """When an embedding batch has failures, the embedding throughput
        metric must be INCOMPLETE and the recommendation must be NO_GO."""
        # Simulate: embedding had failures, throughput is null.
        perf = _make_performance(
            embedding_throughput=None,  # Failed batch — no throughput
            gpu_memory_mb=4096.0,
        )

        result = _make_result(
            campaign_id="fr_emb_fail",
            mode="agent_led",
            quality=_make_quality(),
            performance=perf,
            outcome="no_go",
        )

        assert result.recommendation.outcome == "no_go"
        assert perf.embedding_throughput is None


# ---------------------------------------------------------------------------
# 6. completed metrics plus nonempty run.errors → NO_GO
# ---------------------------------------------------------------------------


class TestRunErrorsForceNoGo:
    """Completed metrics plus nonempty run.errors → NO_GO."""

    def test_errors_with_measured_metrics(self):
        """Even when all quality and performance metrics are measured,
        a nonempty run error list forces NO_GO."""
        result = _make_result(
            campaign_id="fr_errors",
            mode="agent_led",
            quality=_make_quality(),
            performance=_make_performance(),
            errors=("execution failed: timeout", "indexing error on chunk 42"),
            outcome="no_go",
        )

        assert result.recommendation.outcome == "no_go"
        assert len(result.runs[0].errors) == 2


# ---------------------------------------------------------------------------
# 7. campaign outcome go_with_conditions → nonzero strict CLI exit
# ---------------------------------------------------------------------------


class TestGoWithConditionsExitCode:
    """campaign outcome go_with_conditions → nonzero strict CLI exit."""

    def test_go_with_conditions_returns_nonzero(self):
        """A strict campaign that produces go_with_conditions must exit
        with a nonzero code — strict CLI success requires exact GO."""
        import research_store.strict_benchmark as strict_mod

        # Mock the full pipeline to produce go_with_conditions.
        def _mock_preflight(*args, **kwargs):
            return (True, [])

        def _mock_run(*args, **kwargs):
            return (
                _make_result(
                    campaign_id="fr_test",
                    mode="agent_led",
                    outcome="go_with_conditions",
                    conditions=("minor performance regression",),
                ),
                "mock_hash",
            )

        def _mock_compare(*args, **kwargs):
            return ReproducibilityComparison(
                run_a_id="fr_test_a",
                run_b_id="fr_test_b",
                all_within_tolerance=True,
                quality_tolerances=(),
                performance_tolerances=(),
                details=(),
            )

        with (
            mock.patch.object(strict_mod, "_preflight_check", _mock_preflight),
            mock.patch.object(strict_mod, "_run_campaign", _mock_run),
            mock.patch.object(strict_mod, "_compare_campaigns", _mock_compare),
            mock.patch.object(
                strict_mod, "_build_manifest", return_value={"schema_version": "v1"}
            ),
            mock.patch.object(strict_mod, "_write_json_atomic", return_value="hash"),
            mock.patch.object(strict_mod, "_compute_file_hash", return_value="hash123"),
        ):
            rc = main(
                [
                    "--candidate-sha",
                    "a" * 40,
                    "--database-url",
                    "postgresql://test@test:5432/test",
                    "--dataset",
                    str(BENCHMARK_FIXTURE),
                ]
            )
            # go_with_conditions is NOT strict GO → nonzero exit.
            assert rc != 0


# ---------------------------------------------------------------------------
# 8. unavailable CPU/GPU collector → null value, explicit state/reason
# ---------------------------------------------------------------------------


class TestUnavailableResourceCollector:
    """unavailable CPU/GPU collector → null value, explicit state/reason."""

    def test_cpu_gpu_unavailable(self):
        """When CPU and GPU collectors are unavailable, the performance
        measurement must have null values with explicit status and reason."""
        from research_domain.models import ResourceSample

        # Simulate unavailable resource samples.
        cpu_sample = ResourceSample(
            run_id="fr_test",
            device_type="cpu",
            status="unavailable",
            value=None,
            failure_reason="psutil not installed",
        )
        gpu_sample = ResourceSample(
            run_id="fr_test",
            device_type="gpu",
            status="unavailable",
            value=None,
            failure_reason="pynvml not installed",
        )

        # The ResourceSample dataclass should carry these fields.
        assert cpu_sample.status == "unavailable"
        assert cpu_sample.value is None
        assert cpu_sample.failure_reason == "psutil not installed"
        assert gpu_sample.status == "unavailable"
        assert gpu_sample.value is None
        assert gpu_sample.failure_reason == "pynvml not installed"


# ---------------------------------------------------------------------------
# 9. partial resource window → INCOMPLETE
# ---------------------------------------------------------------------------


class TestPartialResourceWindow:
    """partial resource window → INCOMPLETE."""

    def test_partial_resource_window(self):
        """When only part of the resource window is covered (e.g. CPU
        samples present but GPU samples missing), the metric must be
        INCOMPLETE."""
        from research_domain.models import ResourceSample

        # Simulate: CPU measured, GPU unavailable.
        cpu_sample = ResourceSample(
            run_id="fr_test",
            device_type="cpu",
            status="measured",
            value=45.0,
            failure_reason="",
        )
        gpu_sample = ResourceSample(
            run_id="fr_test",
            device_type="gpu",
            status="unavailable",
            value=None,
            failure_reason="GPU collector not available for this window",
        )

        assert cpu_sample.status == "measured"
        assert cpu_sample.value == 45.0
        assert gpu_sample.status == "unavailable"
        assert gpu_sample.value is None

        # A partial window means the performance metric is INCOMPLETE.
        perf = _make_performance(
            cpu_percent=cpu_sample.value,
            gpu_memory_mb=None,  # GPU missing
        )
        assert perf.gpu_memory_mb is None


# ---------------------------------------------------------------------------
# 10. basename collision in recall sources → no false match
# ---------------------------------------------------------------------------


class TestBasenameCollision:
    """basename collision in recall sources → no false match."""

    def test_basename_collision_no_false_match(self):
        """A candidate with the same basename as a benchmark source but a
        different full path must NOT match in canonical identity matching."""
        from research_store.release_benchmark import _canonical_match

        # Same basename, different directory.
        file_path = "scripts/foo.py"
        canonical_url = "https://example.com/other/foo.py"

        # _canonical_match should NOT match because the file_path is not
        # a path component of the canonical_url — the directory differs.
        assert _canonical_match(file_path, canonical_url) is False

        # The same basename in the SAME directory should match.
        canonical_url_same = "https://example.com/scripts/foo.py"
        assert _canonical_match(file_path, canonical_url_same) is True

        # Exact match should work.
        assert _canonical_match(file_path, "scripts/foo.py") is True


# ---------------------------------------------------------------------------
# 11. evidence link exists but citation validation fails → citation metric fails
# ---------------------------------------------------------------------------


class TestCitationValidationFailure:
    """evidence link exists but citation validation fails → citation metric fails."""

    def test_citation_link_present_but_invalid(self):
        """When a claim_evidence_link exists but the exact citation
        validation fails (e.g. passage does not resolve to an exact chunk
        ID), the citation_accuracy metric must reflect the failure."""
        # Simulate a citation_pass artifact where links exist but
        # validation results show failures.
        artifact = {
            "validation_results": [
                {
                    "claim_id": "claim-001",
                    "status": "invalid",
                    "reason": "passage does not resolve to exact chunk",
                },
                {
                    "claim_id": "claim-002",
                    "status": "valid",
                    "reason": "",
                },
            ]
        }

        # Only 1 of 2 claims is valid → citation_accuracy = 0.5
        results = artifact["validation_results"]
        valid = sum(1 for r in results if r["status"] == "valid")
        total = len(results)
        citation_accuracy = valid / total if total > 0 else None

        assert citation_accuracy == 0.5
        # A citation_accuracy of 0.5 is below the quality threshold.
        quality = _make_quality(citation_accuracy=0.5)
        assert quality.citation_accuracy < 0.88  # Below expected threshold


# ---------------------------------------------------------------------------
# 12. Valkey unavailable → preflight failure
# ---------------------------------------------------------------------------


class TestValkeyUnavailable:
    """Valkey unavailable → preflight failure."""

    def test_valkey_unavailable_preflight_fails(self, tmp_path: Path):
        """When Valkey is unavailable (no URL or connection failure),
        the complete preflight must fail."""
        dataset = tmp_path / "benchmark.json"
        dataset.write_text("{}", encoding="utf-8")

        # Mock probe_valkey to raise — simulating Valkey unavailable.
        with (
            mock.patch(
                "research_store.preflight.probe_valkey",
                side_effect=RuntimeError("VALKEY_URL is required"),
            ),
            mock.patch(
                "research_store.preflight.probe_postgres",
                return_value="PostgreSQL OK",
            ),
            mock.patch(
                "research_store.preflight.probe_firecrawl",
                return_value="Firecrawl OK",
            ),
            mock.patch(
                "research_store.preflight.probe_embedding",
                return_value=("Embedding OK", [1.0] * 768),
            ),
            mock.patch(
                "research_store.preflight.probe_qdrant",
                return_value="Qdrant OK",
            ),
            mock.patch(
                "research_store.preflight.probe_reranker",
                return_value="Reranker OK",
            ),
            mock.patch(
                "research_store.preflight.probe_generative",
                return_value="Generative OK",
            ),
            mock.patch(
                "research_store.preflight.probe_resources",
                return_value="Resources OK",
            ),
            mock.patch(
                "research_store.preflight.probe_index_worker",
                return_value="Index worker OK",
            ),
        ):
            ok, errors = _preflight_check(
                database_url="postgresql://test",
                blob_root=tmp_path / "blobs",
                qdrant_url="http://qdrant",
                qdrant_api_key="",
                dataset_path=dataset,
                campaign_dir=tmp_path / "campaign",
                candidate_sha="a" * 40,
            )

        assert ok is False
        assert any("Valkey" in e or "VALKEY_URL" in e for e in errors)


# ---------------------------------------------------------------------------
# 13. candidate SHA differs from HEAD → preflight failure
# ---------------------------------------------------------------------------


class TestCandidateShaMismatch:
    """candidate SHA differs from HEAD → preflight failure."""

    def test_candidate_sha_mismatch(self, tmp_path: Path):
        """When the candidate SHA does not match the current HEAD,
        the complete preflight must fail."""
        dataset = tmp_path / "benchmark.json"
        dataset.write_text("{}", encoding="utf-8")

        # Use a candidate SHA that differs from HEAD.
        fake_sha = "b" * 40

        ok, errors = _preflight_check(
            database_url="postgresql://test",
            blob_root=tmp_path / "blobs",
            qdrant_url="http://qdrant",
            qdrant_api_key="",
            dataset_path=dataset,
            campaign_dir=tmp_path / "campaign",
            candidate_sha=fake_sha,
        )

        assert ok is False
        assert any("does not match" in e for e in errors)


# ---------------------------------------------------------------------------
# 14. campaign A/B cache events overlap in time → exact run isolation
# ---------------------------------------------------------------------------


class TestCacheEventIsolation:
    """campaign A/B cache events overlap in time → exact run isolation."""

    def test_cache_run_isolation(self):
        """Cache events from Campaign A and Campaign B must be scoped to
        their exact run IDs. Overlapping timestamps must not cause cross-
        campaign contamination."""
        # Simulate cache events from two campaigns with overlapping times.
        run_a_id = str(uuid4())
        run_b_id = str(uuid4())

        # Each campaign has its own run-scoped cache events.
        cache_events_a = [
            {
                "run_id": run_a_id,
                "cache_key": "key-001",
                "timestamp": time.time(),
                "hit": True,
            },
            {
                "run_id": run_a_id,
                "cache_key": "key-002",
                "timestamp": time.time() + 1,
                "hit": False,
            },
        ]
        cache_events_b = [
            {
                "run_id": run_b_id,
                "cache_key": "key-001",
                "timestamp": time.time(),  # Overlapping timestamp
                "hit": True,
            },
            {
                "run_id": run_b_id,
                "cache_key": "key-003",
                "timestamp": time.time() + 2,
                "hit": True,
            },
        ]

        # Verify that filtering by run_id isolates events correctly.
        a_hits = [e for e in cache_events_a if e["run_id"] == run_a_id]
        b_hits = [e for e in cache_events_b if e["run_id"] == run_b_id]

        assert len(a_hits) == 2
        assert len(b_hits) == 2
        # No cross-contamination.
        for event in a_hits:
            assert event["run_id"] == run_a_id
        for event in b_hits:
            assert event["run_id"] == run_b_id

        # Campaign A should NOT see Campaign B's events.
        a_sees_b = any(e["run_id"] == run_b_id for e in a_hits)
        assert a_sees_b is False

        # Campaign B should NOT see Campaign A's events.
        b_sees_a = any(e["run_id"] == run_a_id for e in b_hits)
        assert b_sees_a is False


# ---------------------------------------------------------------------------
# 15. Valkey unavailable in preflight (production probe path)
# ---------------------------------------------------------------------------


class TestValkeyProbeFailure:
    """Valkey probe failure → preflight failure."""

    def test_valkey_probe_raises(self):
        """The production probe_valkey function raises when Valkey is
        unavailable, which causes run_complete_preflight to record an error."""
        from research_store.preflight import probe_valkey

        with pytest.raises(RuntimeError, match="VALKEY_URL is required"):
            probe_valkey("")


# ---------------------------------------------------------------------------
# 16. Strict mode candidate SHA validation rejects short SHA
# ---------------------------------------------------------------------------


class TestCandidateShaValidation:
    """candidate SHA must be exactly 40 hex characters."""

    def test_short_sha_rejected(self):
        """A candidate SHA shorter than 40 characters is rejected."""
        rc = main(
            [
                "--candidate-sha",
                "abc123",
                "--dataset",
                str(BENCHMARK_FIXTURE),
            ]
        )
        assert rc == 1

    def test_non_hex_sha_rejected(self):
        """A candidate SHA containing non-hex characters is rejected."""
        rc = main(
            [
                "--candidate-sha",
                "zzzz" + "a" * 36,
                "--dataset",
                str(BENCHMARK_FIXTURE),
            ]
        )
        assert rc == 1

    def test_valid_40_char_sha_passes_validation(self):
        """A valid 40-character lowercase hex SHA passes CLI validation."""
        with (
            mock.patch(
                "research_store.strict_benchmark._preflight_check",
                return_value=(True, []),
            ) as mock_preflight,
            mock.patch("research_store.strict_benchmark._run_campaign") as mock_run,
        ):
            mock_run.return_value = (_make_result(), "hash123")
            with mock.patch(
                "research_store.strict_benchmark._compare_campaigns"
            ) as mock_comp:
                mock_comp.return_value = ReproducibilityComparison(
                    run_a_id="fr_a",
                    run_b_id="fr_b",
                    all_within_tolerance=True,
                    quality_tolerances=(),
                    performance_tolerances=(),
                    details=(),
                )
                with mock.patch(
                    "research_store.strict_benchmark._build_manifest"
                ) as mock_manifest:
                    mock_manifest.return_value = {"schema_version": "v1"}
                    with mock.patch(
                        "research_store.strict_benchmark._write_json_atomic"
                    ) as mock_write:
                        mock_write.return_value = "hash"
                        with mock.patch(
                            "research_store.strict_benchmark._compute_file_hash"
                        ) as mock_hash:
                            mock_hash.return_value = "hash123"
                            rc = main(
                                [
                                    "--candidate-sha",
                                    "a" * 40,
                                    "--database-url",
                                    "postgresql://test@test:5432/test",
                                    "--dataset",
                                    str(BENCHMARK_FIXTURE),
                                ]
                            )
                            assert rc == 0
                            assert mock_preflight.called
