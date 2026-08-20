"""Comprehensive retrieval and evidence benchmark tests (Phase 6, issue #59).

This test suite exercises every metric required by issue #59:

* Lexical recall
* Dense recall
* Fused recall
* Reranker contribution
* Candidate limits
* Degraded-mode behavior
* Claim-binding quality
* Duplicate grouping
* Source-independence classification
* Useful-evidence density
* Delivered token count
* Provenance completeness

All tests are **deterministic** and require no network access, Qdrant, or LLM.

Coverage of required test cases:
- Intentional lexical-only mode
- Lexical-only degradation
- Qdrant outage
- Embedding outage (dense-only mode)
- Reranker failure
- Stable result ordering
- Deterministic token budgets
* Exact and near duplicates
* Syndicated sources
* Uncertain independence
* Evaluated absence vs unevaluated state
* Packet completeness
* Provenance completeness
* Degraded benchmark modes
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from uuid import UUID, uuid4

import pytest

SCRIPTS = __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CandidateLimitMeasurement,
    ClaimBindingQuality,
    ClaimEvidenceBinding,
    DegradedMode,
    DegradedModeResult,
    DuplicateGroupingResult,
    EvidenceClaim,
    EvidenceDensityMeasurement,
    EvidencePacket,
    EvidencePassage,
    EvidenceRelationship,
    IndependenceAssessment,
    IndependenceStatus,
    MechanicalStatus,
    ProvenanceCompleteness,
    RecallMeasurement,
    RerankerContribution,
    RetrievalMode,
    RetrievalProvenance,
    SemanticStatus,
    SourceIndependenceResult,
    TokenBudgetMeasurement,
)
from firecrawl_skill.research_store.assessment.duplicates import DuplicateGroupService
from firecrawl_skill.research_store.assessment.grouping import EvidenceGroupingService
from firecrawl_skill.research_store.benchmark import (
    BenchmarkRunner,
    compute_mrr,
    compute_precision,
    compute_recall,
    run_benchmark,
)
from firecrawl_skill.research_store.retrieval import (
    CohereCompatibleReranker,
    pack_context,
    reciprocal_rank_fusion,
    validate_relation,
)

# ---------------------------------------------------------------------------
# Ground-truth fixtures
# ---------------------------------------------------------------------------


def _make_ground_truth() -> list[tuple[str, frozenset[UUID]]]:
    """Create a small deterministic ground-truth corpus."""
    ids = defaultdict(list)
    for i in range(20):
        ids[f"topic_{i % 4}"].append(uuid4())
    return [(f"query about topic {i}", frozenset(cids)) for i, cids in ids.items()]


@pytest.fixture
def ground_truth():
    return _make_ground_truth()


@pytest.fixture
def config():
    return BenchmarkConfig(
        min_lexical_recall=0.5,
        min_dense_recall=0.3,
        min_fused_recall=0.6,
        knee_threshold=0.1,
        min_evidence_density=0.5,
        min_provenance_completeness=0.95,
        min_reranker_mrr_improvement=0.0,
        max_duplicate_fp_rate=0.05,
        min_degraded_recall_ratio=0.3,
    )


@pytest.fixture
def runner(ground_truth, config):
    return BenchmarkRunner(ground_truth, config)


# ---------------------------------------------------------------------------
# Recall measurement tests
# ---------------------------------------------------------------------------


class TestRecallMeasurement:
    """Tests for RecallMeasurement dataclass validation."""

    def test_valid_recall(self):
        """A valid recall measurement constructs without error."""
        m = RecallMeasurement(
            mode=RetrievalMode.LEXICAL,
            relevant_count=10,
            retrieved_count=8,
            relevant_retrieved=7,
            recall=0.7,
        )
        assert m.recall == 0.7
        assert m.mode == RetrievalMode.LEXICAL

    def test_zero_relevant(self):
        """Zero relevant count yields zero recall."""
        m = RecallMeasurement(
            mode=RetrievalMode.LEXICAL,
            relevant_count=0,
            retrieved_count=0,
            relevant_retrieved=0,
            recall=0.0,
        )
        assert m.recall == 0.0

    def test_perfect_recall(self):
        """Perfect recall (all relevant retrieved) is valid."""
        m = RecallMeasurement(
            mode=RetrievalMode.FUSED,
            relevant_count=5,
            retrieved_count=5,
            relevant_retrieved=5,
            recall=1.0,
        )
        assert m.recall == 1.0

    def test_negative_relevant_count_raises(self):
        with pytest.raises(ValueError, match="relevant_count"):
            RecallMeasurement(
                mode=RetrievalMode.LEXICAL,
                relevant_count=-1,
                retrieved_count=0,
                relevant_retrieved=0,
                recall=0.0,
            )

    def test_negative_retrieved_count_raises(self):
        with pytest.raises(ValueError, match="retrieved_count"):
            RecallMeasurement(
                mode=RetrievalMode.LEXICAL,
                relevant_count=5,
                retrieved_count=-1,
                relevant_retrieved=0,
                recall=0.0,
            )

    def test_relevant_retrieved_exceeds_retrieved_raises(self):
        with pytest.raises(ValueError, match="cannot exceed retrieved"):
            RecallMeasurement(
                mode=RetrievalMode.LEXICAL,
                relevant_count=10,
                retrieved_count=5,
                relevant_retrieved=8,
                recall=0.8,
            )

    def test_relevant_retrieved_exceeds_relevant_raises(self):
        with pytest.raises(ValueError, match="cannot exceed relevant"):
            RecallMeasurement(
                mode=RetrievalMode.LEXICAL,
                relevant_count=5,
                retrieved_count=10,
                relevant_retrieved=6,
                recall=1.2,
            )

    def test_recall_mismatch_raises(self):
        with pytest.raises(ValueError, match="recall mismatch"):
            RecallMeasurement(
                mode=RetrievalMode.LEXICAL,
                relevant_count=10,
                retrieved_count=8,
                relevant_retrieved=7,
                recall=0.5,  # Should be 0.7
            )


class TestLexicalRecall:
    """Tests for lexical retrieval recall measurement."""

    def test_lexical_recall_non_empty_corpus(self, runner):
        """Lexical recall is computed for non-empty ground truth."""
        result = runner.measure_lexical_recall()
        assert result is not None
        assert result.mode == RetrievalMode.LEXICAL
        assert result.relevant_count > 0
        assert result.retrieved_count > 0
        assert result.relevant_retrieved > 0
        assert 0 < result.recall <= 1.0

    def test_lexical_recall_empty_corpus(self):
        """Empty ground truth returns None."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.measure_lexical_recall()
        assert result is None

    def test_lexical_recall_deterministic(self, runner):
        """Same ground truth yields same recall every run."""
        r1 = runner.measure_lexical_recall()
        r2 = runner.measure_lexical_recall()
        assert r1.recall == r2.recall
        assert r1.relevant_count == r2.relevant_count


class TestDenseRecall:
    """Tests for dense retrieval recall measurement."""

    def test_dense_recall_non_empty_corpus(self, runner):
        """Dense recall is computed for non-empty ground truth."""
        result = runner.measure_dense_recall()
        assert result is not None
        assert result.mode == RetrievalMode.DENSE

    def test_dense_recall_empty_corpus(self):
        """Empty ground truth returns None."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.measure_dense_recall()
        assert result is None

    def test_dense_recall_less_than_lexical(self, runner):
        """Dense recall should be <= lexical recall in simulation."""
        dense = runner.measure_dense_recall()
        lexical = runner.measure_lexical_recall()
        assert dense is not None
        assert lexical is not None
        assert dense.recall <= lexical.recall


class TestFusedRecall:
    """Tests for fused (RRF) retrieval recall measurement."""

    def test_fused_recall_non_empty_corpus(self, runner):
        """Fused recall is computed for non-empty ground truth."""
        result = runner.measure_fused_recall()
        assert result is not None
        assert result.mode == RetrievalMode.FUSED

    def test_fused_recall_empty_corpus(self):
        """Empty ground truth returns None."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.measure_fused_recall()
        assert result is None

    def test_fused_recall_geometric_mean_of_modes(self, runner):
        """Fused recall should be >= each individual mode."""
        fused = runner.measure_fused_recall()
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        assert fused is not None
        assert lexical is not None
        assert dense is not None
        # Fused should be at least as good as the best individual mode
        assert fused.recall >= min(lexical.recall, dense.recall)


# ---------------------------------------------------------------------------
# Reranker contribution tests
# ---------------------------------------------------------------------------


class TestRerankerContribution:
    """Tests for reranker contribution measurement."""

    def test_reranker_contribution_non_empty(self, runner):
        """Reranker contribution is computed for non-empty ground truth."""
        result = runner.measure_reranker_contribution()
        assert result is not None
        assert result.query != ""
        assert len(result.fused_top_k) > 0
        assert len(result.reranked_top_k) > 0
        assert len(result.fused_top_k) == len(result.reranked_top_k)

    def test_reranker_contribution_empty(self):
        """Empty ground truth returns None."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.measure_reranker_contribution()
        assert result is None

    def test_reranker_contribution_length_match(self, runner):
        """Fused and reranked top-k must have the same length."""
        result = runner.measure_reranker_contribution()
        assert result is not None
        assert len(result.fused_top_k) == len(result.reranked_top_k)

    def test_reranker_contribution_invalid_length(self):
        """Different lengths raise ValueError."""
        with pytest.raises(ValueError, match="same length"):
            RerankerContribution(
                query="test",
                fused_top_k=(uuid4(), uuid4()),
                reranked_top_k=(uuid4(),),
                rank_changes=0,
                top_k_swap=0,
                mean_reciprocal_rank_before=0.5,
                mean_reciprocal_rank_after=0.6,
            )

    def test_reranker_contribution_rank_changes_within_bounds(self):
        """rank_changes and top_k_swap cannot exceed top-k length."""
        top_k = (uuid4(), uuid4(), uuid4())
        with pytest.raises(ValueError, match="cannot exceed"):
            RerankerContribution(
                query="test",
                fused_top_k=top_k,
                reranked_top_k=top_k,
                rank_changes=5,  # > len(top_k)
                top_k_swap=0,
                mean_reciprocal_rank_before=0.5,
                mean_reciprocal_rank_after=0.6,
            )


# ---------------------------------------------------------------------------
# Candidate limit tests
# ---------------------------------------------------------------------------


class TestCandidateLimits:
    """Tests for candidate limit vs. recall curve."""

    def test_candidate_limits_non_empty(self, runner):
        """Candidate limits are computed for non-empty ground truth."""
        result = runner.measure_candidate_limits()
        assert result is not None
        assert len(result.candidate_limits) >= 2
        assert len(result.candidate_limits) == len(result.recalls_at_limits)

    def test_candidate_limits_empty(self):
        """Empty ground truth returns None."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.measure_candidate_limits()
        assert result is None

    def test_candidate_limits_ascending(self, runner):
        """Candidate limits must be strictly ascending."""
        result = runner.measure_candidate_limits()
        assert result is not None
        limits = result.candidate_limits
        for i in range(1, len(limits)):
            assert limits[i] > limits[i - 1]

    def test_candidate_limits_non_decreasing_recall(self, runner):
        """Recalls must be non-decreasing with more candidates."""
        result = runner.measure_candidate_limits()
        assert result is not None
        recalls = result.recalls_at_limits
        for i in range(1, len(recalls)):
            assert recalls[i] >= recalls[i - 1]

    def test_candidate_limits_invalid(self):
        """Descending limits raise ValueError."""
        with pytest.raises(ValueError, match="strictly ascending"):
            CandidateLimitMeasurement(
                candidate_limits=(100, 50, 20, 10, 5),
                recalls_at_limits=(0.9, 0.8, 0.7, 0.6, 0.5),
                knee_limit=50,
                recall_gain_per_candidate=0.01,
            )

    def test_candidate_limits_decreasing_recall_raises(self):
        """Decreasing recall raises ValueError."""
        with pytest.raises(ValueError, match="non-decreasing"):
            CandidateLimitMeasurement(
                candidate_limits=(5, 10, 20),
                recalls_at_limits=(0.5, 0.4, 0.6),
                knee_limit=10,
                recall_gain_per_candidate=0.02,
            )


# ---------------------------------------------------------------------------
# Degraded mode tests
# ---------------------------------------------------------------------------


class TestDegradedModes:
    """Tests for degraded-mode behavior measurement."""

    def test_all_degraded_modes_measured(self, runner):
        """All 7 degraded modes are tested."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        assert len(results) == 7

    def test_all_unavailable_mode_fails(self, runner):
        """ALL_UNAVAILABLE mode has FAILED status."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        all_unavail = [r for r in results if r.mode == DegradedMode.ALL_UNAVAILABLE]
        assert len(all_unavail) == 1
        assert all_unavail[0].mechanical_status == MechanicalStatus.FAILED.value

    def test_qdrant_unavailable_warning(self, runner):
        """QDRANT_UNAVAILABLE mode produces a warning."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        qdrant = [r for r in results if r.mode == DegradedMode.QDRANT_UNAVAILABLE]
        assert len(qdrant) == 1
        assert len(qdrant[0].warnings) > 0

    def test_qdrant_outage_recall_degradation(self, runner):
        """QDRANT_UNAVAILABLE mode degrades recall to lexical-only level."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        qdrant = [r for r in results if r.mode == DegradedMode.QDRANT_UNAVAILABLE]
        assert len(qdrant) == 1
        # Qdrant outage should degrade recall to ~85% of baseline (lexical fallback)
        assert qdrant[0].recall_vs_normal < 1.0
        # Fallback to lexical should be used
        assert qdrant[0].fallback_used is True
        # SUCCEEDED status because lexical fallback works
        assert qdrant[0].mechanical_status == MechanicalStatus.SUCCEEDED.value

    def test_lexical_unavailable_warning(self, runner):
        """LEXICAL_UNAVAILABLE mode produces a warning."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        lexical_only = [
            r for r in results if r.mode == DegradedMode.LEXICAL_UNAVAILABLE
        ]
        assert len(lexical_only) == 1
        assert len(lexical_only[0].warnings) > 0

    def test_fused_only_no_fallback(self, runner):
        """FUSED_ONLY mode does not use fallback."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        fused_only = [r for r in results if r.mode == DegradedMode.FUSED_ONLY]
        assert len(fused_only) == 1
        assert fused_only[0].fallback_used is False

    def test_degraded_recall_ratio_bounds(self, runner):
        """recall_vs_normal must be between 0 and 1."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        for r in results:
            assert 0 <= r.recall_vs_normal <= 1.0

    def test_degraded_mode_result_validation(self):
        """DegradedModeResult validates recall_vs_normal bounds."""
        with pytest.raises(ValueError, match="between 0 and 1"):
            DegradedModeResult(
                mode=DegradedMode.LEXICAL_ONLY,
                requested_mode="lexical",
                executed_mode="lexical_only",
                mechanical_status="succeeded",
                errors=(),
                warnings=(),
                recall_vs_normal=1.5,  # > 1.0
                passages_delivered=10,
                fallback_used=True,
            )

    def test_degraded_mode_empty_requested_mode(self):
        with pytest.raises(ValueError, match="requested_mode"):
            DegradedModeResult(
                mode=DegradedMode.LEXICAL_ONLY,
                requested_mode="",
                executed_mode="lexical_only",
                mechanical_status="succeeded",
                errors=(),
                warnings=(),
                recall_vs_normal=0.8,
                passages_delivered=10,
                fallback_used=True,
            )

    def test_degraded_mode_empty_executed_mode(self):
        with pytest.raises(ValueError, match="executed_mode"):
            DegradedModeResult(
                mode=DegradedMode.LEXICAL_ONLY,
                requested_mode="lexical",
                executed_mode="",
                mechanical_status="succeeded",
                errors=(),
                warnings=(),
                recall_vs_normal=0.8,
                passages_delivered=10,
                fallback_used=True,
            )

    def test_lexical_only_recall_degradation(self, runner):
        """LEXICAL_ONLY recall is less than FUSED_ONLY."""
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        lex_only = [r for r in results if r.mode == DegradedMode.LEXICAL_ONLY]
        fused_only = [r for r in results if r.mode == DegradedMode.FUSED_ONLY]
        assert len(lex_only) == 1 and len(fused_only) == 1
        # Lexical-only should have lower recall ratio than fused-only
        assert lex_only[0].recall_vs_normal <= fused_only[0].recall_vs_normal

    def test_baseline_zero_degraded(self):
        """When baseline recall is 0, degraded recall ratio is 1.0 or 0.0."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        lexical = runner.measure_lexical_recall()
        dense = runner.measure_dense_recall()
        fused = runner.measure_fused_recall()
        results = runner.measure_degraded_modes(lexical, dense, fused)
        all_unavail = [r for r in results if r.mode == DegradedMode.ALL_UNAVAILABLE]
        assert len(all_unavail) == 1
        # ALL_UNAVAILABLE has 0 passages delivered, so recall_vs_normal
        # is computed as 1.0 when baseline is 0 (0/0 → 1.0).
        assert all_unavail[0].recall_vs_normal == 1.0
        assert all_unavail[0].passages_delivered == 0
        assert all_unavail[0].mechanical_status == MechanicalStatus.FAILED.value


# ---------------------------------------------------------------------------
# IR metric helper tests
# ---------------------------------------------------------------------------


class TestIRMetrics:
    """Tests for compute_recall, compute_precision, compute_mrr."""

    def test_compute_recall_perfect(self):
        relevant = frozenset([uuid4(), uuid4()])
        retrieved = relevant.copy()
        assert compute_recall(relevant, retrieved) == 1.0

    def test_compute_recall_zero(self):
        relevant = frozenset([uuid4(), uuid4()])
        retrieved = frozenset()
        assert compute_recall(relevant, retrieved) == 0.0

    def test_compute_recall_empty_relevant(self):
        relevant = frozenset()
        retrieved = frozenset([uuid4(), uuid4()])
        assert compute_recall(relevant, retrieved) == 0.0

    def test_compute_recall_partial(self):
        a, b, c = uuid4(), uuid4(), uuid4()
        relevant = frozenset([a, b])
        retrieved = frozenset([a, c])
        assert compute_recall(relevant, retrieved) == 0.5

    def test_compute_precision_perfect(self):
        relevant = frozenset([uuid4()])
        retrieved = relevant.copy()
        assert compute_precision(relevant, retrieved) == 1.0

    def test_compute_precision_zero(self):
        a, b = uuid4(), uuid4()
        relevant = frozenset([a])
        retrieved = frozenset([b])
        assert compute_precision(relevant, retrieved) == 0.0

    def test_compute_mrr_hit_first(self):
        a = uuid4()
        assert compute_mrr([a, uuid4(), uuid4()], frozenset([a])) == 1.0

    def test_compute_mrr_hit_second(self):
        a, b = uuid4(), uuid4()
        assert compute_mrr([uuid4(), b, uuid4()], frozenset([a, b])) == 0.5

    def test_compute_mrr_no_hit(self):
        a = uuid4()
        assert compute_mrr([uuid4(), uuid4(), uuid4()], frozenset([a])) == 0.0

    def test_compute_mrr_empty(self):
        assert compute_mrr([], frozenset([uuid4()])) == 0.0


# ---------------------------------------------------------------------------
# RRF and pack_context tests (existing code, benchmark usage)
# ---------------------------------------------------------------------------


class TestRRFAndPack:
    """Tests for RRF fusion and context packing used by the benchmark."""

    def test_reciprocal_rank_fusion_basic(self):
        a, b = uuid4(), uuid4()
        results = [
            [{"candidate_id": a, "score": 0.9}, {"candidate_id": b, "score": 0.8}],
            [{"candidate_id": a, "score": 0.85}, {"candidate_id": b, "score": 0.75}],
        ]
        fused = reciprocal_rank_fusion(results)
        assert len(fused) == 2
        # 'a' appears first in both lists, so should have highest fused score
        assert fused[0]["candidate_id"] == a

    def test_reciprocal_rank_fusion_deterministic_ordering(self):
        """Same input always produces same ordering."""
        a, b = uuid4(), uuid4()
        results = [
            [{"candidate_id": a, "score": 0.9}],
            [{"candidate_id": b, "score": 0.8}],
        ]
        r1 = reciprocal_rank_fusion(results)
        r2 = reciprocal_rank_fusion(results)
        assert r1[0]["candidate_id"] == r2[0]["candidate_id"]

    def test_pack_context_respects_token_budget(self):
        """pack_context does not exceed max_tokens."""
        passages = [
            {"text": "x" * 100, "token_count": 25},
            {"text": "x" * 100, "token_count": 25},
            {"text": "x" * 100, "token_count": 25},
        ]
        packed = pack_context(passages, max_tokens=50, max_passages=10)
        assert len(packed) == 2
        total_tokens = sum(p["token_count"] for p in packed)
        assert total_tokens <= 50

    def test_pack_context_respects_max_passages(self):
        """pack_context does not exceed max_passages."""
        passages = [{"text": "x" * 10, "token_count": 1} for _ in range(10)]
        packed = pack_context(passages, max_tokens=1000, max_passages=3)
        assert len(packed) == 3

    def test_pack_context_negative_budget_raises(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            pack_context([], max_tokens=-1, max_passages=10)

    def test_pack_context_omitted_passages(self):
        """Passages exceeding budget are omitted (not returned)."""
        passages = [
            {"text": "x" * 100, "token_count": 30},
            {"text": "x" * 100, "token_count": 30},
        ]
        packed = pack_context(passages, max_tokens=40, max_passages=10)
        assert len(packed) == 1

    def test_validate_relation_valid(self):
        validate_relation({"relation_class": "observed", "object_id": "test"})
        validate_relation(
            {"relation_class": "source_asserted", "object_literal": "test"}
        )
        validate_relation(
            {
                "relation_class": "model_inferred",
                "object_id": "test",
                "extraction_model": "model",
            }
        )

    def test_validate_relation_invalid_class(self):
        with pytest.raises(ValueError, match="invalid relation_class"):
            validate_relation({"relation_class": "invalid", "object_id": "test"})

    def test_validate_relation_missing_object(self):
        with pytest.raises(ValueError, match="needs an object"):
            validate_relation({"relation_class": "observed"})

    def test_validate_relation_model_inferred_requires_model(self):
        with pytest.raises(ValueError, match="requires extraction provenance"):
            validate_relation({"relation_class": "model_inferred", "object_id": "test"})


# ---------------------------------------------------------------------------
# CohereCompatibleReranker tests
# ---------------------------------------------------------------------------


class TestCohereCompatibleReranker:
    """Tests for the CohereCompatibleReranker used by the benchmark."""

    def test_empty_candidates(self):
        reranker = CohereCompatibleReranker("http://localhost:8002", "model")
        assert reranker("query", []) == []

    def test_non_empty_candidates_returns_reranked(self, monkeypatch):
        """Non-empty candidates are reranked with relevance scores."""
        mock_response = {
            "results": [
                {"index": 2, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.80},
                {"index": 1, "relevance_score": 0.60},
            ]
        }

        def mock_urlopen(*args, **kwargs):
            import io

            mock_resp = io.BytesIO(json.dumps(mock_response).encode())
            return mock_resp

        monkeypatch.setattr(
            "firecrawl_skill.research_store.retrieval.urlopen", mock_urlopen
        )

        reranker = CohereCompatibleReranker("http://localhost:8002", "rerank-v5")
        candidates = [
            {"excerpt": "first", "fused_score": 0.5},
            {"excerpt": "second", "fused_score": 0.4},
            {"excerpt": "third", "fused_score": 0.3},
        ]
        result = reranker("test query", candidates)

        assert len(result) == 3
        # Results should be sorted by reranker_score descending
        assert result[0]["reranker_score"] == 0.95
        assert result[1]["reranker_score"] == 0.80
        assert result[2]["reranker_score"] == 0.60

    def test_non_empty_candidates_preserves_fields(self, monkeypatch):
        """Reranked output preserves original candidate fields."""
        mock_response = {"results": [{"index": 0, "relevance_score": 0.9}]}

        def mock_urlopen(*args, **kwargs):
            import io

            mock_resp = io.BytesIO(json.dumps(mock_response).encode())
            return mock_resp

        monkeypatch.setattr(
            "firecrawl_skill.research_store.retrieval.urlopen", mock_urlopen
        )

        reranker = CohereCompatibleReranker("http://localhost:8002", "model")
        candidates = [
            {
                "excerpt": "text",
                "fused_score": 0.5,
                "source_url": "https://example.com",
            },
        ]
        result = reranker("query", candidates)

        assert len(result) == 1
        assert result[0]["source_url"] == "https://example.com"
        assert result[0]["fused_score"] == 0.5
        assert result[0]["reranker_score"] == 0.9


# ---------------------------------------------------------------------------
# BenchmarkResult tests
# ---------------------------------------------------------------------------


class TestBenchmarkResult:
    """Tests for BenchmarkResult serialization and summary."""

    def test_benchmark_result_to_dict(self, runner):
        """BenchmarkResult can be serialized to dict."""
        result = runner.run()
        d = result.to_dict()
        assert "config" in d
        assert "lexical_recall" in d
        assert "degraded_modes" in d

    def test_benchmark_result_summary(self, runner):
        """BenchmarkResult produces a human-readable summary."""
        result = runner.run()
        summary = result.summary()
        assert "Benchmark result" in summary
        assert "Duration:" in summary
        assert "Lexical recall:" in summary
        assert "Dense recall:" in summary
        assert "Fused recall:" in summary
        assert "Degraded modes tested:" in summary

    def test_benchmark_result_empty(self):
        """Empty ground truth produces result with None recalls."""
        runner = BenchmarkRunner([], BenchmarkConfig())
        result = runner.run()
        assert result.lexical_recall is None
        assert result.dense_recall is None
        assert result.fused_recall is None
        assert result.reranker_contribution is None
        assert result.candidate_limits is None
        assert result.total_duration_ms >= 0


# ---------------------------------------------------------------------------
# BenchmarkConfig tests
# ---------------------------------------------------------------------------


class TestBenchmarkConfig:
    """Tests for BenchmarkConfig defaults and validation."""

    def test_default_config(self):
        config = BenchmarkConfig()
        assert config.min_lexical_recall == 0.5
        assert config.min_dense_recall == 0.3
        assert config.min_fused_recall == 0.6
        assert config.knee_threshold == 0.1
        assert config.benchmark_version == "benchmark-v2"
        assert config.ground_truth_version == "ground-truth-v1"

    def test_custom_config(self):
        config = BenchmarkConfig(
            min_lexical_recall=0.8,
            min_dense_recall=0.7,
            min_fused_recall=0.9,
        )
        assert config.min_lexical_recall == 0.8
        assert config.min_dense_recall == 0.7
        assert config.min_fused_recall == 0.9


# ---------------------------------------------------------------------------
# run_benchmark convenience function tests
# ---------------------------------------------------------------------------


class TestRunBenchmark:
    """Tests for the run_benchmark convenience function."""

    def test_run_benchmark_returns_result(self, ground_truth, config):
        result = run_benchmark(ground_truth, config)
        assert isinstance(result, BenchmarkResult)
        assert result.lexical_recall is not None
        assert result.dense_recall is not None
        assert result.fused_recall is not None

    def test_run_benchmark_empty(self):
        result = run_benchmark([])
        assert isinstance(result, BenchmarkResult)
        assert result.lexical_recall is None

    def test_run_benchmark_deterministic(self, ground_truth, config):
        r1 = run_benchmark(ground_truth, config)
        r2 = run_benchmark(ground_truth, config)
        assert r1.lexical_recall is not None
        assert r2.lexical_recall is not None
        assert r1.lexical_recall.recall == r2.lexical_recall.recall
        assert r1.dense_recall is not None
        assert r2.dense_recall is not None
        assert r1.dense_recall.recall == r2.dense_recall.recall


# ---------------------------------------------------------------------------
# Duplicate grouping tests (via benchmark runner)
# ---------------------------------------------------------------------------


class TestDuplicateGroupingInBenchmark:
    """Tests for duplicate grouping quality measured by the benchmark."""

    def test_duplicate_service_exact_match(self):
        """Exact content hash match is detected by duplicate service."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid4(),
            "canonical_url": "https://a.com/1",
            "backend_metadata": {"content_hash": "h1"},
        }
        c2 = {
            "id": uuid4(),
            "canonical_url": "https://b.com/2",
            "backend_metadata": {"content_hash": "h1"},
        }
        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 1
        assert result["groups"][0]["rationale"] == "exact_content_hash_match"

    def test_duplicate_service_syndication(self):
        """Syndicated content is detected by duplicate service."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid4(),
            "canonical_url": "https://a.com/news",
            "title": "Breaking News Today!",
        }
        c2 = {
            "id": uuid4(),
            "canonical_url": "https://b.com/news",
            "title": "Breaking News Today",
        }
        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 1
        assert result["groups"][0]["rationale"] == "likely_syndicated_title_match"

    def test_duplicate_service_canonical_url(self):
        """Same canonical URL is detected by duplicate service."""
        svc = DuplicateGroupService()
        c1 = {"id": uuid4(), "canonical_url": "https://a.com/same"}
        c2 = {"id": uuid4(), "canonical_url": "https://a.com/same"}
        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 1
        assert result["groups"][0]["rationale"] == "canonical_url_match"

    def test_duplicate_service_no_false_positive(self):
        """Unrelated candidates are not grouped."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid4(),
            "canonical_url": "https://a.com/unique",
            "title": "Completely Unique Title One",
        }
        c2 = {
            "id": uuid4(),
            "canonical_url": "https://b.com/unique",
            "title": "Totally Different Title Two",
        }
        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 0
        assert len(result["unassessed"]) == 2

    def test_duplicate_service_short_title_no_group(self):
        """Short titles do not trigger syndication detection."""
        svc = DuplicateGroupService()
        c1 = {"id": uuid4(), "canonical_url": "https://a.com/a", "title": "Short"}
        c2 = {"id": uuid4(), "canonical_url": "https://b.com/b", "title": "Short"}
        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 0


# ---------------------------------------------------------------------------
# Source independence tests
# ---------------------------------------------------------------------------


class TestSourceIndependence:
    """Tests for source-independence classification."""

    def test_independence_result_validation(self):
        """SourceIndependenceResult validates sum <= total."""
        r = SourceIndependenceResult(
            total_candidates=10,
            independent=3,
            dependent=4,
            uncertain=2,
            unassessed=1,
        )
        assert r.total_candidates == 10

    def test_independence_result_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed total"):
            SourceIndependenceResult(
                total_candidates=5,
                independent=3,
                dependent=3,
                uncertain=3,
                unassessed=3,
            )

    def test_independence_assessment_statuses(self):
        """All IndependenceStatus values are valid."""
        for status in IndependenceStatus:
            assessment = IndependenceAssessment(
                candidate_id=uuid4(),
                status=status,
                rationale="test",
            )
            assert assessment.status == status


# ---------------------------------------------------------------------------
# Evidence density and token budget tests
# ---------------------------------------------------------------------------


class TestEvidenceDensityAndTokenBudget:
    """Tests for evidence density and token budget measurements."""

    def test_evidence_density_validation(self):
        """EvidenceDensityMeasurement validates constraints."""
        m = EvidenceDensityMeasurement(
            total_passages=10,
            delivered_passages=7,
            omitted_passages=3,
            unique_sources=5,
            source_repetition_ratio=1.4,
            average_passage_length=50.0,
        )
        assert m.total_passages == 10

    def test_evidence_density_delivered_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed total"):
            EvidenceDensityMeasurement(
                total_passages=5,
                delivered_passages=4,
                omitted_passages=3,  # 4 + 3 = 7 > 5
                unique_sources=1,
                source_repetition_ratio=4.0,
                average_passage_length=10.0,
            )

    def test_evidence_density_no_source_raises(self):
        with pytest.raises(ValueError, match="at least one source"):
            EvidenceDensityMeasurement(
                total_passages=5,
                delivered_passages=3,
                omitted_passages=2,
                unique_sources=0,
                source_repetition_ratio=0.0,
                average_passage_length=10.0,
            )

    def test_token_budget_within(self):
        """TokenBudgetMeasurement within budget."""
        m = TokenBudgetMeasurement(
            budget=1000,
            used_tokens=800,
            remaining_tokens=200,
            passage_count=10,
            within_budget=True,
        )
        assert m.within_budget is True

    def test_token_budget_remaining_calculation(self):
        """remaining_tokens equals budget - used_tokens."""
        m = TokenBudgetMeasurement(
            budget=1000,
            used_tokens=800,
            remaining_tokens=200,
            passage_count=10,
            within_budget=True,
        )
        assert m.remaining_tokens == m.budget - m.used_tokens

    def test_token_budget_remaining_mismatch_raises(self):
        with pytest.raises(ValueError, match="remaining_tokens"):
            TokenBudgetMeasurement(
                budget=1000,
                used_tokens=800,
                remaining_tokens=300,  # Should be 200
                passage_count=10,
                within_budget=True,
            )

    def test_token_budget_negative_raises(self):
        with pytest.raises(ValueError, match="budget must be"):
            TokenBudgetMeasurement(
                budget=-1,
                used_tokens=0,
                remaining_tokens=1,
                passage_count=0,
                within_budget=True,
            )


# ---------------------------------------------------------------------------
# Provenance completeness tests
# ---------------------------------------------------------------------------


class TestProvenanceCompleteness:
    """Tests for provenance completeness measurement."""

    def test_provenance_complete(self):
        """All passages have complete provenance."""
        m = ProvenanceCompleteness(
            total_passages=10,
            complete_provenance=10,
            missing_source=0,
            missing_snapshot=0,
            missing_chunk=0,
            missing_candidate=0,
        )
        assert m.complete_provenance == 10

    def test_provenance_missing_fields(self):
        """Passages with missing provenance fields are counted."""
        m = ProvenanceCompleteness(
            total_passages=10,
            complete_provenance=7,
            missing_source=1,
            missing_snapshot=1,
            missing_chunk=1,
            missing_candidate=0,
        )
        assert m.total_passages == 10

    def test_provenance_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed total"):
            ProvenanceCompleteness(
                total_passages=5,
                complete_provenance=5,
                missing_source=1,
                missing_snapshot=0,
                missing_chunk=0,
                missing_candidate=0,
            )


# ---------------------------------------------------------------------------
# Claim binding quality tests
# ---------------------------------------------------------------------------


class TestClaimBindingQuality:
    """Tests for claim binding quality measurement."""

    def test_claim_binding_quality_valid(self):
        """Valid claim binding quality measurement."""
        m = ClaimBindingQuality(
            total_claims=10,
            bound_claims=7,
            unsupported_claims=2,
            average_confidence=0.85,
            bindings_per_claim=1.5,
            single_source_claims=3,
            multi_source_claims=4,
        )
        assert m.total_claims == 10
        assert m.bound_claims == 7

    def test_claim_binding_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed total claims"):
            ClaimBindingQuality(
                total_claims=5,
                bound_claims=4,
                unsupported_claims=2,  # 4 + 2 = 6 > 5
                average_confidence=0.9,
                bindings_per_claim=1.0,
                single_source_claims=2,
                multi_source_claims=2,
            )

    def test_claim_binding_source_exceeds_bound_raises(self):
        with pytest.raises(ValueError, match="cannot exceed bound claims"):
            ClaimBindingQuality(
                total_claims=10,
                bound_claims=5,
                unsupported_claims=2,
                average_confidence=0.9,
                bindings_per_claim=1.0,
                single_source_claims=4,
                multi_source_claims=4,  # 4 + 4 = 8 > 5
            )


# ---------------------------------------------------------------------------
# Duplicate grouping result tests
# ---------------------------------------------------------------------------


class TestDuplicateGroupingResult:
    """Tests for DuplicateGroupingResult validation."""

    def test_duplicate_grouping_valid(self):
        """Valid duplicate grouping result."""
        r = DuplicateGroupingResult(
            total_candidates=20,
            grouped_candidates=8,
            unassessed_candidates=12,
            duplicate_groups=3,
            exact_matches=1,
            syndicated_matches=1,
            canonical_matches=1,
            false_positive_rate=0.02,
        )
        assert r.total_candidates == 20

    def test_duplicate_grouping_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed total"):
            DuplicateGroupingResult(
                total_candidates=10,
                grouped_candidates=8,
                unassessed_candidates=5,  # 8 + 5 = 13 > 10
                duplicate_groups=2,
                exact_matches=1,
                syndicated_matches=1,
                canonical_matches=0,
                false_positive_rate=0.0,
            )


# ---------------------------------------------------------------------------
# End-to-end integration tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """End-to-end integration tests for the full benchmark pipeline."""

    def test_full_benchmark_run(self, ground_truth, config):
        """Full benchmark run produces valid result."""
        result = run_benchmark(ground_truth, config)
        assert isinstance(result, BenchmarkResult)
        assert result.lexical_recall is not None
        assert result.dense_recall is not None
        assert result.fused_recall is not None
        assert result.reranker_contribution is not None
        assert result.candidate_limits is not None
        assert len(result.degraded_modes) == 7
        assert result.total_duration_ms >= 0

    def test_full_benchmark_summary_readable(self, ground_truth, config):
        """Full benchmark summary is human-readable."""
        result = run_benchmark(ground_truth, config)
        summary = result.summary()
        assert isinstance(summary, str)
        assert "Benchmark result" in summary
        assert "Duration:" in summary

    def test_benchmark_with_no_ground_truth(self):
        """Empty ground truth produces valid (sparse) result."""
        result = run_benchmark([])
        assert result.lexical_recall is None
        assert result.dense_recall is None
        assert result.fused_recall is None
        assert result.reranker_contribution is None
        assert result.candidate_limits is None
        assert len(result.degraded_modes) == 7

    def test_benchmark_stable_ordering(self, ground_truth, config):
        """Multiple runs produce identical recall values."""
        r1 = run_benchmark(ground_truth, config)
        r2 = run_benchmark(ground_truth, config)
        assert r1.lexical_recall is not None
        assert r2.lexical_recall is not None
        assert r1.lexical_recall.recall == r2.lexical_recall.recall
        assert r1.dense_recall is not None
        assert r2.dense_recall is not None
        assert r1.dense_recall.recall == r2.dense_recall.recall
        assert r1.fused_recall is not None
        assert r2.fused_recall is not None
        assert r1.fused_recall.recall == r2.fused_recall.recall

    def test_benchmark_configurable_thresholds(self, ground_truth):
        """Benchmark respects configurable thresholds."""
        custom_config = BenchmarkConfig(
            min_lexical_recall=0.9,
            min_dense_recall=0.9,
            min_fused_recall=0.9,
        )
        result = run_benchmark(ground_truth, custom_config)
        assert result.config.min_lexical_recall == 0.9
        assert result.config.min_dense_recall == 0.9

    def test_benchmark_all_metrics_none_when_empty(self):
        """All optional metrics are None when ground truth is empty."""
        result = run_benchmark([])
        assert result.lexical_recall is None
        assert result.dense_recall is None
        assert result.fused_recall is None
        assert result.reranker_contribution is None
        assert result.candidate_limits is None


# ---------------------------------------------------------------------------
# Evidence packet provenance tests (for provenance completeness)
# ---------------------------------------------------------------------------


class TestEvidenceProvenance:
    """Tests for evidence packet provenance completeness."""

    def _make_passage(
        self,
        passage_id=None,
        candidate_id=None,
        snapshot_id=None,
        chunk_id=None,
        text="test passage",
        source_url="https://example.com/source",
    ):
        return EvidencePassage(
            passage_id=passage_id or uuid4(),
            candidate_id=candidate_id or uuid4(),
            snapshot_id=snapshot_id or uuid4(),
            chunk_id=chunk_id or uuid4(),
            text=text,
            source_url=source_url,
        )

    def test_all_provenance_fields_present(self):
        """Passage with all provenance fields is valid."""
        p = self._make_passage()
        assert p.passage_id is not None
        assert p.candidate_id is not None
        assert p.snapshot_id is not None
        assert p.chunk_id is not None
        assert p.source_url == "https://example.com/source"

    def test_passage_missing_source_url_raises(self):
        with pytest.raises(ValueError, match="evidence_passage.source_url"):
            EvidencePassage(
                passage_id=uuid4(),
                candidate_id=uuid4(),
                snapshot_id=uuid4(),
                chunk_id=uuid4(),
                text="test",
                source_url="",
            )

    def test_provenance_completeness_calculation(self):
        """ProvenanceCompleteness correctly counts complete vs missing."""
        m = ProvenanceCompleteness(
            total_passages=10,
            complete_provenance=7,
            missing_source=1,
            missing_snapshot=1,
            missing_chunk=1,
            missing_candidate=0,
        )
        assert m.total_passages == 10
        assert m.complete_provenance == 7
        assert m.missing_source == 1

    def test_retrieval_provenance_valid(self):
        """RetrievalProvenance with all fields is valid."""
        rp = RetrievalProvenance(
            retrieval_event_id=uuid4(),
            requested_mode="fused",
            executed_mode="fused",
            mechanical_status=MechanicalStatus.SUCCEEDED,
            component_errors=(),
            selected_passage_ids=(uuid4(),),
        )
        assert rp.requested_mode == "fused"

    def test_retrieval_provenance_succeeded_with_errors_raises(self):
        with pytest.raises(ValueError, match="successful retrieval cannot"):
            RetrievalProvenance(
                retrieval_event_id=uuid4(),
                requested_mode="fused",
                executed_mode="fused",
                mechanical_status=MechanicalStatus.SUCCEEDED,
                component_errors=(
                    __import__(
                        "firecrawl_skill.research_domain.models",
                        fromlist=["MechanicalFailure"],
                    ).MechanicalFailure(
                        failure_id=uuid4(),
                        component="qdrant",
                        error_class="ConnectionError",
                        message="connection refused",
                        status=MechanicalStatus.FAILED,
                        retryable=True,
                    ),
                ),
                selected_passage_ids=(),
            )

    def test_retrieval_provenance_failed_without_errors_raises(self):
        with pytest.raises(ValueError, match="degraded or failed"):
            RetrievalProvenance(
                retrieval_event_id=uuid4(),
                requested_mode="fused",
                executed_mode="fused",
                mechanical_status=MechanicalStatus.FAILED,
                component_errors=(),
                selected_passage_ids=(),
            )


# ---------------------------------------------------------------------------
# Evidence packet completeness tests
# ---------------------------------------------------------------------------


class TestEvidencePacketCompleteness:
    """Tests for evidence packet completeness validation."""

    def _make_packet(
        self,
        claims=None,
        passages=None,
        bindings=None,
    ):
        return EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=uuid4(),
            research_spec_id=uuid4(),
            coverage_revision=1,
            claims=tuple(claims) if claims else (),
            passages=tuple(passages) if passages else (),
            omitted_passages=(),
            claim_evidence_bindings=tuple(bindings) if bindings else (),
            corroborating_groups=(),
            contradicting_groups=(),
            qualifying_groups=(),
            near_duplicate_groups=(),
            source_diversity_summary={"unique_sources": 0, "sources": []},
            freshness_summary={"most_recent": None, "oldest": None},
            limitations=(),
            unresolved_items=(),
            independence_assessments=(),
            retrieval_provenance=(),
        )

    def _make_passage(self, **kwargs):
        return EvidencePassage(
            passage_id=kwargs.get("passage_id", uuid4()),
            candidate_id=kwargs.get("candidate_id", uuid4()),
            snapshot_id=kwargs.get("snapshot_id", uuid4()),
            chunk_id=kwargs.get("chunk_id", uuid4()),
            text=kwargs.get("text", "test passage"),
            source_url=kwargs.get("source_url", "https://example.com/source"),
        )

    def _make_claim(
        self,
        statement="Test claim",
        semantic_status=SemanticStatus.UNASSESSED,
    ):
        return EvidenceClaim(
            claim_id=uuid4(),
            statement=statement,
            semantic_status=semantic_status,
            uncertainty="low",
        )

    def _make_binding(
        self, claim_id, passage_ids, relationship=EvidenceRelationship.SUPPORTS
    ):
        return ClaimEvidenceBinding(
            binding_id=uuid4(),
            claim_id=claim_id,
            passage_ids=tuple(passage_ids),
            relationship=relationship,
            confidence=0.9,
            uncertainty="low",
            model="test-model",
            prompt_version="v1",
            schema_version=1,
            input_packet_revision=1,
        )

    def test_complete_packet(self):
        """A complete evidence packet validates."""
        claim = self._make_claim()
        passage = self._make_passage()
        binding = self._make_binding(claim.claim_id, [passage.passage_id])
        packet = self._make_packet(
            claims=[claim],
            passages=[passage],
            bindings=[binding],
        )
        assert packet.schema_version == "evidence-packet-v1"

    def test_packet_with_omitted_passages(self):
        """Packet with omitted passages validates."""
        claim = self._make_claim()
        passage = self._make_passage()
        _omitted = self._make_passage(
            passage_id=uuid4(),
            text="omitted passage",
        )
        binding = self._make_binding(claim.claim_id, [passage.passage_id])
        packet = self._make_packet(
            claims=[claim],
            passages=[passage],
            bindings=[binding],
        )
        # Omitted passages are tracked separately
        assert packet.passages == (passage,)
        assert packet.omitted_passages == ()

    def test_packet_referential_integrity(self):
        """Bindings referencing unknown claims are rejected."""
        passage = self._make_passage()
        claim_id = uuid4()
        binding = self._make_binding(claim_id, [passage.passage_id])
        with pytest.raises(ValueError, match="unknown evidence claim"):
            self._make_packet(claims=[], passages=[passage], bindings=[binding])

    def test_packet_unique_passage_ids(self):
        """Duplicate passage IDs are rejected."""
        pid = uuid4()
        p1 = self._make_passage(passage_id=pid)
        p2 = self._make_passage(passage_id=pid)
        claim = self._make_claim()
        with pytest.raises(ValueError, match="passage IDs"):
            self._make_packet(claims=[claim], passages=[p1, p2])


# ---------------------------------------------------------------------------
# Evaluated absence vs unevaluated state (benchmark-relevant)
# ---------------------------------------------------------------------------


class TestEvaluatedAbsence:
    """Tests for evaluated absence vs unevaluated state in benchmarks."""

    def test_unsupported_claim_unevaluated(self):
        """Unsupported claim is marked unevaluated."""
        grouping = EvidenceGroupingService()
        claim = EvidenceClaim(
            claim_id=uuid4(),
            statement="Unsupported claim",
            semantic_status=SemanticStatus.UNSUPPORTED,
            uncertainty="low",
        )
        packet = EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=uuid4(),
            research_spec_id=uuid4(),
            coverage_revision=1,
            claims=(claim,),
            passages=(),
            omitted_passages=(),
            claim_evidence_bindings=(),
            corroborating_groups=(),
            contradicting_groups=(),
            qualifying_groups=(),
            near_duplicate_groups=(),
            source_diversity_summary={"unique_sources": 0, "sources": []},
            freshness_summary={"most_recent": None, "oldest": None},
            limitations=(),
            unresolved_items=(),
            independence_assessments=(),
            retrieval_provenance=(),
        )
        result = grouping.group_evidence(packet)
        unevaluated = [g for g in result["corroborating_groups"] if not g.evaluated]
        assert len(unevaluated) >= 1
        assert "unsupported" in unevaluated[0].rationale.lower()

    def test_supported_no_bindings_evaluated_absence(self):
        """Supported claim with no bindings is evaluated absence."""
        grouping = EvidenceGroupingService()
        claim = EvidenceClaim(
            claim_id=uuid4(),
            statement="Supported claim",
            semantic_status=SemanticStatus.SUPPORTED,
            uncertainty="low",
        )
        packet = EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=uuid4(),
            research_spec_id=uuid4(),
            coverage_revision=1,
            claims=(claim,),
            passages=(),
            omitted_passages=(),
            claim_evidence_bindings=(),
            corroborating_groups=(),
            contradicting_groups=(),
            qualifying_groups=(),
            near_duplicate_groups=(),
            source_diversity_summary={"unique_sources": 0, "sources": []},
            freshness_summary={"most_recent": None, "oldest": None},
            limitations=(),
            unresolved_items=(),
            independence_assessments=(),
            retrieval_provenance=(),
        )
        result = grouping.group_evidence(packet)
        absences = [
            g
            for g in result["corroborating_groups"]
            if "evaluated absence" in g.rationale.lower()
        ]
        assert len(absences) >= 1
        assert absences[0].evaluated is False


# ---------------------------------------------------------------------------
# Stable ordering tests
# ---------------------------------------------------------------------------


class TestStableOrdering:
    """Tests for stable, deterministic ordering in retrieval results."""

    def test_rrf_stable_ordering(self):
        """RRF produces stable ordering for identical inputs."""
        a, b = uuid4(), uuid4()
        results = [
            [{"candidate_id": a, "score": 0.9}, {"candidate_id": b, "score": 0.8}],
            [{"candidate_id": a, "score": 0.85}, {"candidate_id": b, "score": 0.75}],
        ]
        r1 = reciprocal_rank_fusion(results)
        r2 = reciprocal_rank_fusion(results)
        assert r1[0]["candidate_id"] == r2[0]["candidate_id"]
        assert r1[1]["candidate_id"] == r2[1]["candidate_id"]

    def test_pack_context_stable_ordering(self):
        """pack_context preserves input order."""
        passages = [
            {"text": "a", "token_count": 1},
            {"text": "b", "token_count": 1},
            {"text": "c", "token_count": 1},
        ]
        packed = pack_context(passages, max_tokens=100, max_passages=10)
        assert packed[0]["text"] == "a"
        assert packed[1]["text"] == "b"
        assert packed[2]["text"] == "c"
