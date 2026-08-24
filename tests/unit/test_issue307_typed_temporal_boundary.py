"""Issue #307 regressions for the typed temporal evidence/recovery boundary."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain.models import MechanicalStatus
from firecrawl_skill.research_store.evidence_preparation_service import (
    EvidencePreparationError,
    EvidencePreparationService,
)
from firecrawl_skill.research_store.temporal_coverage import (
    TemporalCoverageUnsatisfied,
)


class _Corpus:
    def __init__(self, passages):
        self.passages = passages

    def select_run_passages(self, *_args, **_kwargs):
        return (
            SimpleNamespace(mechanical_status=MechanicalStatus.SUCCEEDED),
            self.passages,
        )


def _service(passages):
    return EvidencePreparationService(
        corpus_service=_Corpus(passages),
        evidence_service=object(),
        coverage_service=object(),
        semantic_service=object(),
        config=SimpleNamespace(),
    )


def test_temporal_insufficiency_is_distinct_from_generic_evidence_failure() -> None:
    assert not issubclass(TemporalCoverageUnsatisfied, EvidencePreparationError)


def test_evidence_boundary_raises_typed_gap_with_bounded_diagnostics() -> None:
    run_id = uuid4()
    chunk_id = uuid4()
    candidate_id = uuid4()
    service = _service(
        [
            {
                "chunk_id": chunk_id,
                "published_at": None,
                "updated_at": None,
                "retrieved_at": "2026-08-23T00:00:00Z",
            }
        ]
    )

    with pytest.raises(TemporalCoverageUnsatisfied) as caught:
        service.prepare(
            run_id=run_id,
            run_revision=4,
            spec={
                "time_window": {"start": None, "end": None},
                "freshness_requirements": [{"max_age_days": 5}],
            },
            research_spec_id=uuid4(),
            coverage_revision=3,
            extracted_assets=[
                {"candidate_id": str(candidate_id), "chunk_ids": [str(chunk_id)]}
            ],
            coverage_items=[],
        )

    diagnostics = caught.value.diagnostics
    assert diagnostics.basis == "freshness"
    assert diagnostics.examined_passages == 1
    assert diagnostics.qualifying_passages == 0
    assert diagnostics.missing_freshness_authority == 1
    assert diagnostics.retrieval_only_passages == 1
    gap = caught.value.to_gap(coverage_revision=3)
    assert gap["kind"] == "temporal_coverage_gap"
    assert gap["automatic_scope_relaxation"] is False
    assert gap["coverage_revision"] == 3


def test_generic_evidence_failure_remains_generic_when_no_temporal_gap_exists() -> None:
    run_id = uuid4()
    chunk_id = uuid4()
    candidate_id = uuid4()
    service = _service(
        [
            {
                "chunk_id": chunk_id,
                "published_at": None,
                "updated_at": None,
                "retrieved_at": "2026-08-23T00:00:00Z",
            }
        ]
    )

    with pytest.raises(EvidencePreparationError, match="no question or claim"):
        service.prepare(
            run_id=run_id,
            run_revision=4,
            spec={
                "time_window": {"start": None, "end": None},
                "freshness_requirements": [],
            },
            research_spec_id=uuid4(),
            coverage_revision=3,
            extracted_assets=[
                {"candidate_id": str(candidate_id), "chunk_ids": [str(chunk_id)]}
            ],
            coverage_items=[],
        )
