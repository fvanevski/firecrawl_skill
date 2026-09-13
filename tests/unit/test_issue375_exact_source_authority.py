"""Issue #375 exact canonical-source authority regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain.models import MechanicalStatus
from firecrawl_skill.research_store.assessment.coverage import CoverageService
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.evidence_preparation_service import (
    EvidencePreparationService,
)
from firecrawl_skill.research_store.exact_source_authority import (
    ExactSourceCoverageUnsatisfied,
    candidate_identity_map,
    canonical_source_identity,
    requirement_candidate_groups,
)
from firecrawl_skill.research_store.research_controller_contract import ResearchResult
from firecrawl_skill.research_store.semantic_service import SemanticCallService
from firecrawl_skill.research_store.smart_objective_intent import (
    materialize_smart_objective_intent,
)


class _Corpus:
    def __init__(self, passages: list[dict[str, Any]]) -> None:
        self.passages = passages

    def select_run_passages(self, *_args: Any, **_kwargs: Any):
        return (
            SimpleNamespace(mechanical_status=MechanicalStatus.SUCCEEDED),
            self.passages,
        )


class _Coverage:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def apply_event(self, *_args: Any, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))


def _service(passages: list[dict[str, Any]], coverage: _Coverage):
    return EvidencePreparationService(
        corpus_service=cast(CorpusService, _Corpus(passages)),
        evidence_service=object(),
        coverage_service=cast(CoverageService, coverage),
        semantic_service=cast(SemanticCallService, object()),
        config=SimpleNamespace(),
    )


def _temporal_none() -> dict[str, Any]:
    return {
        "kind": "none",
        "relative_quantity": None,
        "relative_unit": None,
        "freshness_basis": None,
        "temporal_basis": "none",
        "publication_start": None,
        "publication_end": None,
        "event_start": None,
        "event_end": None,
        "as_of": None,
        "uncertainty": "none",
        "rationale": "no temporal restriction",
    }


def _intent(*, exact_url: str | None) -> dict[str, Any]:
    return {
        "schema_version": "smart-objective-intent-v2",
        "objective": "Use the required canonical page as evidentiary authority",
        "research_questions": ["What does the required page establish?"],
        "entities": [],
        "jurisdictions": [],
        "user_constraints": [],
        "exact_source_requirements": (
            [{"canonical_url": exact_url}] if exact_url is not None else []
        ),
        "temporal": _temporal_none(),
        "assumptions": [],
        "ambiguities": [],
    }


def test_structured_exact_source_materializes_separately_from_generic_source_class() -> None:
    materialized = materialize_smart_objective_intent(
        _intent(exact_url="https://www.example.com/canonical/"),
        execution_mode="autonomous_local",
        evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )

    assert len(materialized.spec.exact_source_requirements) == 1
    assert (
        materialized.spec.exact_source_requirements[0].canonical_url
        == "https://www.example.com/canonical"
    )
    assert materialized.spec.required_source_classes
    assert materialized.spec.user_constraints == ()


def test_no_exact_constraint_keeps_exact_source_requirements_empty() -> None:
    materialized = materialize_smart_objective_intent(
        _intent(exact_url=None),
        execution_mode="autonomous_local",
        evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert materialized.spec.exact_source_requirements == ()


def test_canonical_identity_accepts_same_resource_normalization_not_other_path() -> None:
    requirement_id = str(uuid4())
    candidate_id = uuid4()
    identities = candidate_identity_map(
        [
            {
                "candidate_id": str(candidate_id),
                "requested_url": "https://example.com:443/canonical/",
            }
        ]
    )
    groups = requirement_candidate_groups(
        [
            {
                "requirement_id": requirement_id,
                "canonical_url": "https://example.com/canonical",
            }
        ],
        identities,
    )
    assert groups[requirement_id] == frozenset({candidate_id})
    assert canonical_source_identity("https://example.com/help/canonical") not in identities[
        candidate_id
    ]


def test_same_vendor_substitute_cannot_satisfy_exact_source_obligation() -> None:
    run_id = uuid4()
    exact_requirement_id = uuid4()
    exact_coverage_id = uuid4()
    substitute_candidate = uuid4()
    substitute_chunk = uuid4()
    coverage = _Coverage()
    service = _service(
        [
            {
                "chunk_id": substitute_chunk,
                "source_url": "https://example.com/help/canonical",
            }
        ],
        coverage,
    )

    with pytest.raises(ExactSourceCoverageUnsatisfied) as caught:
        service.prepare(
            run_id=run_id,
            run_revision=2,
            spec={
                "time_window": {"start": None, "end": None},
                "freshness_requirements": [],
                "exact_source_requirements": [
                    {
                        "requirement_id": str(exact_requirement_id),
                        "canonical_url": "https://example.com/canonical",
                    }
                ],
            },
            research_spec_id=uuid4(),
            coverage_revision=1,
            extracted_assets=[
                {
                    "candidate_id": str(substitute_candidate),
                    "requested_url": "https://example.com/help/canonical",
                    "chunk_ids": [str(substitute_chunk)],
                }
            ],
            coverage_items=[
                {
                    "coverage_item_id": str(exact_coverage_id),
                    "item_type": "exact_source_requirement",
                    "subject_id": str(exact_requirement_id),
                }
            ],
        )

    state = caught.value.states[0]
    assert state.acquired is False
    assert state.selected is False
    assert state.reason == "required_exact_source_not_acquired"
    assert coverage.events == []


def test_temporally_unqualified_exact_source_is_context_only_not_satisfying() -> None:
    exact_requirement_id = uuid4()
    exact_coverage_id = uuid4()
    candidate_id = uuid4()
    chunk_id = uuid4()
    coverage = _Coverage()
    service = _service(
        [
            {
                "chunk_id": chunk_id,
                "source_url": "https://example.com/canonical",
                "published_at": None,
                "updated_at": None,
                "retrieved_at": "2026-09-13T00:00:00Z",
            }
        ],
        coverage,
    )

    with pytest.raises(ExactSourceCoverageUnsatisfied) as caught:
        service.prepare(
            run_id=uuid4(),
            run_revision=2,
            spec={
                "time_window": {"start": None, "end": None},
                "freshness_requirements": [{"max_age_days": 30}],
                "exact_source_requirements": [
                    {
                        "requirement_id": str(exact_requirement_id),
                        "canonical_url": "https://example.com/canonical",
                    }
                ],
            },
            research_spec_id=uuid4(),
            coverage_revision=1,
            extracted_assets=[
                {
                    "candidate_id": str(candidate_id),
                    "requested_url": "https://example.com/canonical/",
                    "snapshot_id": str(uuid4()),
                    "chunk_ids": [str(chunk_id)],
                }
            ],
            coverage_items=[
                {
                    "coverage_item_id": str(exact_coverage_id),
                    "item_type": "exact_source_requirement",
                    "subject_id": str(exact_requirement_id),
                }
            ],
        )

    state = caught.value.states[0]
    assert state.acquired is True
    assert state.selected is False
    assert state.reason == "required_exact_source_temporally_unqualified"
    assert coverage.events[0]["new_status"] == "acquired"


def test_public_result_carries_structured_source_compliance() -> None:
    run_id = f"fr_{uuid4().hex}"
    compliance = {
        "required": True,
        "overall_status": "acquired_not_selected",
        "requirements": [
            {
                "canonical_url": "https://example.com/canonical",
                "discovered": True,
                "acquired": True,
                "selected": False,
                "satisfied": False,
                "status": "acquired_not_selected",
                "selected_source_urls": [],
            }
        ],
    }
    result = ResearchResult(
        schema_version="research-result-v3",
        run_id=run_id,
        objective="objective",
        lifecycle_state="coverage_review",
        lifecycle_revision=3,
        disposition="operator_action_required",
        terminal=False,
        outcome=None,
        result_ready=False,
        handoff_ready=False,
        objective_satisfied=False,
        source_compliance=compliance,
    )

    assert result.to_dict()["source_compliance"] == compliance
