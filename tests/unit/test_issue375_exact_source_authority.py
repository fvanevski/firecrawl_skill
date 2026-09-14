"""Issue #375 exact canonical-source authority regressions."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_domain.codec import to_dict
from firecrawl_skill.research_domain.models import (
    ExactSourceRequirement,
    MechanicalStatus,
)
from firecrawl_skill.research_store.assessment.binding import ClaimBindingService
from firecrawl_skill.research_store.assessment.coverage import CoverageService
from firecrawl_skill.research_store.assessment.evidence import EvidenceService
from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY
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
from firecrawl_skill.research_store.read_models import (
    CandidateRecord,
    ExtractedAssetRecord,
)
from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import ResearchResult
from firecrawl_skill.research_store.resume_state_repository import (
    PostgresResumeStateReader,
)
from firecrawl_skill.research_store.semantic_service import (
    HostArtifactResult,
    SemanticCallService,
)
from firecrawl_skill.research_store.smart_objective_intent import (
    SmartObjectiveIntentError,
    interpret_smart_objective,
    materialize_smart_objective_intent,
)


class _Corpus:
    def __init__(self, passages: list[dict[str, Any]]) -> None:
        self.passages = passages

    def select_run_passages(self, _run_id: UUID, chunk_ids: list[UUID], **_kwargs: Any):
        by_chunk = {UUID(str(item["chunk_id"])): item for item in self.passages}
        return (
            SimpleNamespace(
                mechanical_status=MechanicalStatus.SUCCEEDED,
                execution_id=uuid4(),
                requested_mode="run_scoped",
                executed_mode="run_scoped",
            ),
            [by_chunk[value] for value in chunk_ids if value in by_chunk],
        )


class _Coverage:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def apply_event(self, *_args: Any, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))

    def apply_evidence_retrieved(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def apply_source_class_observed(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def apply_freshness_observed(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _service(passages: list[dict[str, Any]], coverage: _Coverage):
    return EvidencePreparationService(
        corpus_service=cast(CorpusService, _Corpus(passages)),
        evidence_service=object(),
        coverage_service=cast(CoverageService, coverage),
        semantic_service=cast(SemanticCallService, object()),
        config=SimpleNamespace(),
    )


def _asset(
    candidate_id: UUID,
    requested_url: str,
    chunk_ids: list[UUID] | tuple[UUID, ...],
    *,
    snapshot_id: UUID | None = None,
    canonical_url: str | None = None,
    final_url: str | None = None,
    ordinal: int = 0,
) -> ExtractedAssetRecord:
    return ExtractedAssetRecord(
        extraction_attempt_id=uuid4(),
        candidate_id=candidate_id,
        snapshot_id=snapshot_id or uuid4(),
        requested_url=requested_url,
        chunk_ids=tuple(chunk_ids),
        final_url=final_url,
        canonical_url=canonical_url,
        ordinal=ordinal,
    )


def _candidate_record(candidate_id: UUID, url: str, *, run_id: UUID | None = None) -> CandidateRecord:
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    return CandidateRecord(
        candidate_id=candidate_id,
        run_id=run_id or uuid4(),
        canonical_url=url,
        canonical_url_sha256="a" * 64,
        original_url=url,
        title=None,
        snippet=None,
        domain="example.com",
        backend="firecrawl",
        published_at=None,
        date_signals={},
        backend_metadata={},
        recurrence_count=1,
        duplicate_group_id=None,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        independence_assessment=None,
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


def test_structured_exact_source_materializes_separately_from_generic_source_class() -> (
    None
):
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


def test_exact_source_requirement_count_is_bounded_in_domain_validation() -> None:
    payload = _intent(exact_url=None)
    payload["exact_source_requirements"] = [
        {"canonical_url": f"https://example.com/required/{index}"}
        for index in range(17)
    ]

    with pytest.raises(
        SmartObjectiveIntentError,
        match="exact_source_requirements exceeds deterministic bound of 16",
    ):
        materialize_smart_objective_intent(
            payload,
            execution_mode="autonomous_local",
            evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        )


def test_explicit_research_spec_rejects_more_than_sixteen_exact_sources() -> None:
    materialized = materialize_smart_objective_intent(
        _intent(exact_url=None),
        execution_mode="autonomous_local",
        evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    requirements = tuple(
        ExactSourceRequirement(
            uuid4(),
            f"https://example.com/explicit/{index}",
        )
        for index in range(17)
    )

    with pytest.raises(
        ValueError,
        match="ResearchSpec exact_source_requirements exceeds deterministic bound of 16",
    ):
        replace(materialized.spec, exact_source_requirements=requirements)


def test_objective_interpreter_projects_oneof_out_of_provider_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objective = "Use exactly https://example.com/canonical as evidentiary authority"
    captured: dict[str, Any] = {}

    def fake_call_authorized_structured(**kwargs: Any):
        captured["schema"] = kwargs["schema"]
        invalid = _intent(exact_url="https://example.com/canonical")
        invalid["objective"] = objective
        invalid["temporal"] = {
            **invalid["temporal"],
            "kind": "relative_freshness",
            "relative_quantity": 7,
            "relative_unit": "day",
            "freshness_basis": "publication_or_update",
            "temporal_basis": "none",
        }
        with pytest.raises(SmartObjectiveIntentError):
            kwargs["post_validate"](invalid)
        valid = _intent(exact_url="https://example.com/canonical")
        valid["objective"] = objective
        kwargs["post_validate"](valid)
        return SimpleNamespace(
            value=valid,
            error=None,
            provenance={},
            semantic_call_id=None,
            artifact_ids=(),
            attempts=(),
        )

    monkeypatch.setattr(
        "firecrawl_skill.research_store.smart_objective_intent.call_authorized_structured",
        fake_call_authorized_structured,
    )
    result = interpret_smart_objective(
        semantic_service=SimpleNamespace(host_artifact_supplier=None),
        status=SimpleNamespace(
            id=uuid4(), lifecycle_revision=0, execution_mode="autonomous_local"
        ),
        objective=objective,
        invocation_id="issue375-provider-schema",
        evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )

    temporal_schema = captured["schema"]["properties"]["temporal"]
    assert "oneOf" not in temporal_schema
    assert result.error is None
    assert result.value["exact_source_requirements"] == [
        {"canonical_url": "https://example.com/canonical"}
    ]


def test_canonical_identity_accepts_same_resource_normalization_not_other_path() -> (
    None
):
    requirement_id = str(uuid4())
    candidate_id = uuid4()
    identities = candidate_identity_map(
        [
            _asset(
                candidate_id,
                "https://www.example.com:443/canonical/",
                [uuid4()],
                canonical_url="https://example.com/canonical",
            )
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
    assert (
        canonical_source_identity("https://example.com/help/canonical")
        not in identities[candidate_id]
    )


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
                _asset(
                    substitute_candidate,
                    "https://example.com/help/canonical",
                    [substitute_chunk],
                )
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
                _asset(
                    candidate_id,
                    "https://example.com/canonical/",
                    [chunk_id],
                )
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


class _MemoryEvidence:
    def __init__(self) -> None:
        self._builder = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)
        self.packets: dict[int, Any] = {}

    def build_evidence_packet(self, *args: Any, **kwargs: Any):
        return self._builder.build_evidence_packet(*args, **kwargs)

    def persist_packet(self, packet: Any) -> int:
        revision = max(self.packets, default=0) + 1
        self.packets[revision] = packet
        return revision

    def export_packet(self, _run_id: UUID, revision: int | None = None):
        if not self.packets:
            return None
        resolved = revision if revision is not None else max(self.packets)
        packet = self.packets[resolved]
        return {
            "packet_revision": resolved,
            "coverage_revision": packet.coverage_revision,
            "payload": to_dict(packet),
        }

    def group_evidence(self, _run_id: UUID, revision: int | None = None) -> int:
        if revision is None:
            return max(self.packets)
        return revision


class _SemanticUOW:
    class runs:
        @staticmethod
        def get_run_status(*, run_id: UUID) -> dict[str, Any]:
            return {"lifecycle_revision": 2, "execution_mode": "autonomous_local"}

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Semantic:
    host_artifact_supplier = None

    def uow_factory(self) -> _SemanticUOW:
        return _SemanticUOW()


class _NoopClaimManifest:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def create_claim(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def create_evidence_link(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _fixture_result(*_args: Any, **kwargs: Any) -> HostArtifactResult:
    return HostArtifactResult(
        value=deepcopy(kwargs["deterministic_fixture"]),
        provenance={},
        attempts=(),
    )


def _selector_result(*_args: Any, **kwargs: Any) -> HostArtifactResult:
    if kwargs["semantic_context"]["stage"] != "exact_source_passage_selection":
        return _fixture_result(*_args, **kwargs)
    payload = json.loads(kwargs["user_prompt"])
    selections = []
    for item in payload["coverage_items"]:
        assert item["text"] == "What does the required page establish?"
        for requirement in payload["exact_source_requirements"]:
            selected = next(
                passage
                for passage in requirement["passages"]
                if "required fact" in passage["text"]
            )
            selections.append(
                {
                    "coverage_item_id": item["coverage_item_id"],
                    "requirement_id": requirement["requirement_id"],
                    "source_passage_id": selected["passage_id"],
                    "evidence_usable": True,
                    "rationale": "directly states the required fact",
                }
            )
    return HostArtifactResult(
        value={"selections": selections}, provenance={}, attempts=()
    )


def _unusable_selector_result(*_args: Any, **kwargs: Any) -> HostArtifactResult:
    return _fixture_result(*_args, **kwargs)


def _context_binding_result(*_args: Any, **kwargs: Any) -> HostArtifactResult:
    payload = deepcopy(kwargs["deterministic_fixture"])
    for evaluation in payload["evaluations"]:
        evaluation["semantic_status"] = "qualified"
        for binding in evaluation["bindings"]:
            binding["relationship"] = "context"
    return HostArtifactResult(value=payload, provenance={}, attempts=())


def _full_preparation_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selection_result=_selector_result,
    binding_result=_fixture_result,
):
    materialized = materialize_smart_objective_intent(
        _intent(exact_url="https://example.com/canonical"),
        execution_mode="autonomous_local",
        evaluated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    spec = to_dict(materialized.spec)
    exact_requirement = materialized.spec.exact_source_requirements[0]
    question = materialized.spec.questions[0]
    exact_candidate = uuid4()
    exact_intro_chunk = uuid4()
    exact_relevant_chunk = uuid4()
    exact_snapshot = uuid4()
    substitute_candidate = uuid4()
    substitute_chunk = uuid4()
    substitute_snapshot = uuid4()
    retrieved_at = datetime(2026, 9, 13, tzinfo=timezone.utc)
    passages = [
        {
            "chunk_id": exact_intro_chunk,
            "candidate_id": exact_candidate,
            "snapshot_id": exact_snapshot,
            "url": "https://example.com/canonical",
            "source_url": "https://example.com/canonical",
            "text": "The first exact-source chunk contains only navigation context.",
            "published_at": None,
            "updated_at": None,
            "retrieved_at": retrieved_at,
        },
        {
            "chunk_id": exact_relevant_chunk,
            "candidate_id": exact_candidate,
            "snapshot_id": exact_snapshot,
            "url": "https://example.com/canonical",
            "source_url": "https://example.com/canonical",
            "text": "The exact canonical source states the required fact.",
            "published_at": None,
            "updated_at": None,
            "retrieved_at": retrieved_at,
        },
        {
            "chunk_id": substitute_chunk,
            "candidate_id": substitute_candidate,
            "snapshot_id": substitute_snapshot,
            "url": "https://example.com/help/canonical",
            "source_url": "https://example.com/help/canonical",
            "text": "A same-vendor substitute discusses related context.",
            "published_at": None,
            "updated_at": None,
            "retrieved_at": retrieved_at,
        },
    ]
    assets = [
        _asset(
            substitute_candidate,
            "https://example.com/help/canonical",
            [substitute_chunk],
            snapshot_id=substitute_snapshot,
            ordinal=0,
        ),
        _asset(
            exact_candidate,
            "https://www.example.com/canonical/",
            [exact_intro_chunk, exact_relevant_chunk],
            snapshot_id=exact_snapshot,
            canonical_url="https://example.com/canonical",
            ordinal=1,
        ),
    ]
    coverage_items = [
        {
            "coverage_item_id": str(uuid4()),
            "item_type": "question",
            "subject_id": str(question.question_id),
        },
        {
            "coverage_item_id": str(uuid4()),
            "item_type": "exact_source_requirement",
            "subject_id": str(exact_requirement.requirement_id),
            "text": exact_requirement.canonical_url,
        },
    ]
    coverage = _Coverage()
    evidence = _MemoryEvidence()
    semantic = _Semantic()
    monkeypatch.setattr(
        "firecrawl_skill.research_store.evidence_preparation_service.call_structured",
        selection_result,
    )
    monkeypatch.setattr(
        "firecrawl_skill.research_store.assessment.binding.call_structured",
        binding_result,
    )
    monkeypatch.setattr(
        "firecrawl_skill.research_store.evidence_preparation_service.ClaimManifestService",
        _NoopClaimManifest,
    )
    service = EvidencePreparationService(
        corpus_service=cast(CorpusService, _Corpus(passages)),
        evidence_service=evidence,
        coverage_service=cast(CoverageService, coverage),
        semantic_service=cast(SemanticCallService, semantic),
        config=SimpleNamespace(generative_model="test-model"),
    )
    return {
        "service": service,
        "spec": spec,
        "research_spec_id": materialized.spec.research_spec_id,
        "assets": assets,
        "coverage_items": coverage_items,
        "coverage": coverage,
        "evidence": evidence,
        "exact_intro_chunk": exact_intro_chunk,
        "exact_chunk": exact_relevant_chunk,
        "substitute_chunk": substitute_chunk,
    }


def test_exact_source_is_bound_even_when_higher_ranked_substitute_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _full_preparation_fixture(monkeypatch)
    result = fixture["service"].prepare(
        run_id=uuid4(),
        run_revision=2,
        spec=fixture["spec"],
        research_spec_id=fixture["research_spec_id"],
        coverage_revision=1,
        extracted_assets=fixture["assets"],
        coverage_items=fixture["coverage_items"],
    )

    packet = fixture["evidence"].packets[result.packet_revision]
    assert {passage.passage_id for passage in packet.passages} == {
        fixture["exact_intro_chunk"],
        fixture["exact_chunk"],
        fixture["substitute_chunk"],
    }
    assert packet.claim_evidence_bindings
    assert all(
        set(binding.passage_ids) == {fixture["exact_chunk"]}
        for binding in packet.claim_evidence_bindings
    )
    assert any(
        event.get("item_type") == "exact_source_requirement"
        and event.get("new_status") == "satisfied"
        for event in fixture["coverage"].events
    )


def test_required_exact_passages_keep_independent_binding_relationships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = str(uuid4())
    passage_a = str(uuid4())
    passage_b = str(uuid4())
    persisted: list[dict[str, Any]] = []

    class _Evidence:
        @staticmethod
        def persist_packet(packet: dict[str, Any]) -> int:
            persisted.append(packet)
            return 2

    monkeypatch.setattr(
        "firecrawl_skill.research_store.assessment.binding.load_model",
        lambda payload: payload,
    )
    service = ClaimBindingService(
        cast(SemanticCallService, object()),
        cast(EvidenceService, _Evidence()),
    )
    packet = {
        "claims": [
            {
                "claim_id": claim_id,
                "semantic_status": "unassessed",
            }
        ],
        "passages": [
            {"passage_id": passage_a},
            {"passage_id": passage_b},
        ],
        "claim_evidence_bindings": [],
    }
    revision = service._process_evaluations(
        packet_dict=packet,
        evaluations=[
            {
                "claim_id": claim_id,
                "semantic_status": "supported",
                "bindings": [
                    {
                        "passage_ids": [passage_a],
                        "relationship": "supports",
                        "confidence": 0.9,
                        "uncertainty": "",
                    },
                    {
                        "passage_ids": [passage_b],
                        "relationship": "context",
                        "confidence": 0.7,
                        "uncertainty": "second exact source does not support this claim",
                    },
                ],
            }
        ],
        model_name="test-model",
        prompt_version="claim-binding-v1",
        schema_version=1,
        packet_revision=1,
        required_passage_ids_by_claim={claim_id: [passage_a, passage_b]},
    )

    assert revision == 2
    bindings = persisted[0]["claim_evidence_bindings"]
    assert [binding["passage_ids"] for binding in bindings] == [
        [passage_a],
        [passage_b],
    ]
    assert [binding["relationship"] for binding in bindings] == [
        "supports",
        "context",
    ]

    with pytest.raises(ValueError, match="bindings must be singleton"):
        service._process_evaluations(
            packet_dict=packet,
            evaluations=[
                {
                    "claim_id": claim_id,
                    "semantic_status": "supported",
                    "bindings": [
                        {
                            "passage_ids": [passage_a, passage_b],
                            "relationship": "supports",
                            "confidence": 0.9,
                            "uncertainty": "",
                        }
                    ],
                }
            ],
            model_name="test-model",
            prompt_version="claim-binding-v1",
            schema_version=1,
            packet_revision=1,
            required_passage_ids_by_claim={claim_id: [passage_a, passage_b]},
        )


def test_context_only_exact_source_binding_cannot_satisfy_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _full_preparation_fixture(
        monkeypatch,
        binding_result=_context_binding_result,
    )
    with pytest.raises(ExactSourceCoverageUnsatisfied) as caught:
        fixture["service"].prepare(
            run_id=uuid4(),
            run_revision=2,
            spec=fixture["spec"],
            research_spec_id=fixture["research_spec_id"],
            coverage_revision=1,
            extracted_assets=fixture["assets"],
            coverage_items=fixture["coverage_items"],
        )

    state = caught.value.states[0]
    assert state.acquired is True
    assert state.selected is True
    assert state.satisfied is False
    assert state.reason == "required_exact_source_not_evidentially_usable"
    assert not any(
        event.get("item_type") == "exact_source_requirement"
        and event.get("new_status") == "satisfied"
        for event in fixture["coverage"].events
    )


def test_acquired_exact_source_that_cannot_support_claim_remains_unsatisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _full_preparation_fixture(
        monkeypatch,
        selection_result=_unusable_selector_result,
    )
    with pytest.raises(ExactSourceCoverageUnsatisfied) as caught:
        fixture["service"].prepare(
            run_id=uuid4(),
            run_revision=2,
            spec=fixture["spec"],
            research_spec_id=fixture["research_spec_id"],
            coverage_revision=1,
            extracted_assets=fixture["assets"],
            coverage_items=fixture["coverage_items"],
        )

    state = caught.value.states[0]
    assert state.acquired is True
    assert state.selected is False
    assert state.satisfied is False
    assert state.reason == "required_exact_source_not_evidentially_usable"


def test_exact_source_selector_batches_declared_maximum_pair_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic_items = [
        {
            "coverage_item_id": str(uuid4()),
            "item_type": "question",
            "subject_id": str(uuid4()),
            "text": f"Question {index}",
        }
        for index in range(12)
    ]
    exact_requirements = [
        {
            "requirement_id": str(uuid4()),
            "canonical_url": f"https://example.com/canonical/{index}",
        }
        for index in range(16)
    ]
    exact_passages: dict[str, list[dict[str, Any]]] = {}
    exact_groups: dict[str, frozenset[UUID]] = {}
    for requirement in exact_requirements:
        requirement_id = str(requirement["requirement_id"])
        exact_passages[requirement_id] = [
            {
                "chunk_id": uuid4(),
                "url": requirement["canonical_url"],
                "text": f"Evidence for {requirement_id}",
            }
        ]
        exact_groups[requirement_id] = frozenset({uuid4()})

    calls: list[dict[str, Any]] = []

    def fake_selector(*_args: Any, **kwargs: Any) -> HostArtifactResult:
        calls.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        assert len(payload["coverage_items"]) == 1
        item = payload["coverage_items"][0]
        selections = [
            {
                "coverage_item_id": item["coverage_item_id"],
                "requirement_id": requirement["requirement_id"],
                "source_passage_id": requirement["passages"][0]["passage_id"],
                "evidence_usable": True,
                "rationale": "direct evidence",
            }
            for requirement in payload["exact_source_requirements"]
        ]
        return HostArtifactResult(
            value={"selections": selections}, provenance={}, attempts=()
        )

    monkeypatch.setattr(
        "firecrawl_skill.research_store.evidence_preparation_service.call_structured",
        fake_selector,
    )
    service = EvidencePreparationService(
        corpus_service=cast(CorpusService, _Corpus([])),
        evidence_service=object(),
        coverage_service=cast(CoverageService, _Coverage()),
        semantic_service=cast(
            SemanticCallService, SimpleNamespace(host_artifact_supplier=None)
        ),
        config=SimpleNamespace(generative_model="test-model"),
    )

    selected = service._select_exact_source_passages(
        run_id=uuid4(),
        run_revision=2,
        coverage_revision=1,
        semantic_items=semantic_items,
        exact_requirements=exact_requirements,
        exact_passages=exact_passages,
        exact_groups=exact_groups,
    )

    assert len(selected) == 12
    assert all(len(passages) == 16 for passages in selected.values())
    assert len(calls) == 24
    assert (
        sum(call["schema"]["properties"]["selections"]["maxItems"] for call in calls)
        == 192
    )
    assert all(
        call["schema"]["properties"]["selections"]["maxItems"] <= 8 for call in calls
    )
    assert all(call["max_output_tokens"] <= 2048 for call in calls)


def test_link_only_substitute_does_not_prove_exact_source_identity() -> None:
    requirement_id = str(uuid4())
    candidate_id = uuid4()
    identities = candidate_identity_map(
        [
            _asset(
                candidate_id,
                "https://example.com/help/canonical",
                [uuid4()],
            )
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
    assert groups[requirement_id] == frozenset()


class _CompliancePacketRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {"payload": self.payload}


class _ComplianceUOW:
    def __init__(
        self,
        *,
        spec: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
        assets: list[tuple[Any, ...]],
        packet: dict[str, Any] | None = None,
    ) -> None:
        self.runs = SimpleNamespace(
            get_research_spec=lambda _run_id: (
                {"payload": spec} if spec is not None else None
            )
        )
        self.candidates = SimpleNamespace(
            list_candidates=lambda _run_id: list(candidates)
        )
        self.evidence_packets = SimpleNamespace(
            get_evidence_packet=lambda _run_id: (
                _CompliancePacketRecord(packet) if packet is not None else None
            )
        )
        self.snapshots = SimpleNamespace(
            resume_assets_for_run=lambda _run_id: list(assets)
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _controller_for_compliance(
    *,
    spec: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    assets: list[tuple[Any, ...]],
) -> ResearchWorkflowController:
    controller = object.__new__(ResearchWorkflowController)
    controller.run_service = SimpleNamespace(
        uow_factory=lambda: _ComplianceUOW(
            spec=spec,
            candidates=candidates,
            assets=assets,
        )
    )
    return controller


def test_public_projection_is_unknown_until_research_spec_exists() -> None:
    status = SimpleNamespace(id=uuid4())
    compliance = _controller_for_compliance(
        spec=None,
        candidates=[],
        assets=[],
    )._source_compliance(status)

    assert compliance is None


def test_public_projection_distinguishes_discovered_acquired_and_not_discovered() -> (
    None
):
    run_id = uuid4()
    requirement_id = uuid4()
    candidate_id = uuid4()
    snapshot_id = uuid4()
    chunk_id = uuid4()
    attempt_id = uuid4()
    spec = {
        "exact_source_requirements": [
            {
                "requirement_id": str(requirement_id),
                "canonical_url": "https://example.com/canonical",
            }
        ]
    }
    status = SimpleNamespace(id=run_id)
    discovered = _controller_for_compliance(
        spec=spec,
        candidates=[
            {
                "id": candidate_id,
                "canonical_url": "https://example.com/canonical",
                "original_url": "https://example.com/canonical",
            }
        ],
        assets=[],
    )._source_compliance(status)
    acquired = _controller_for_compliance(
        spec=spec,
        candidates=[],
        assets=[
            (
                attempt_id,
                candidate_id,
                snapshot_id,
                "https://example.com/canonical",
                [chunk_id],
                "https://example.com/canonical",
                "https://example.com/canonical",
            )
        ],
    )._source_compliance(status)
    not_discovered = _controller_for_compliance(
        spec=spec,
        candidates=[],
        assets=[],
    )._source_compliance(status)

    assert discovered is not None
    assert discovered["overall_status"] == "discovered_not_acquired"
    assert discovered["requirements"][0]["discovered"] is True
    assert discovered["requirements"][0]["acquired"] is False
    assert acquired is not None
    assert acquired["overall_status"] == "acquired_not_selected"
    assert acquired["requirements"][0]["acquired"] is True
    assert acquired["requirements"][0]["selected"] is False
    assert not_discovered is not None
    assert not_discovered["overall_status"] == "not_discovered"
    assert not_discovered["requirements"][0]["acquired"] is False


def test_public_projection_recognizes_durable_redirect_alias_before_packet() -> None:
    requirement_id = uuid4()
    candidate_id = uuid4()
    spec = {
        "exact_source_requirements": [
            {
                "requirement_id": str(requirement_id),
                "canonical_url": "https://example.com/canonical",
            }
        ]
    }
    compliance = _controller_for_compliance(
        spec=spec,
        candidates=[],
        assets=[
            (
                uuid4(),
                candidate_id,
                uuid4(),
                "https://example.com/legacy-entry",
                [uuid4()],
                "https://example.com/canonical",
                "https://example.com/canonical",
            )
        ],
    )._source_compliance(SimpleNamespace(id=uuid4()))

    assert compliance is not None
    assert compliance["overall_status"] == "acquired_not_selected"
    projected = compliance["requirements"][0]
    assert projected["discovered"] is True
    assert projected["acquired"] is True
    assert projected["selected"] is False


class _EventRuns:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def list_events(
        self, _run_id: UUID, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        return self.events[offset : offset + limit]


class _EventUOW:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.runs = _EventRuns(events)

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def test_exact_source_gap_replays_from_durable_events_without_reinterpretation() -> (
    None
):
    run_id = uuid4()
    gap = {
        "kind": "exact_source_coverage_gap",
        "status": "unsatisfied",
        "requirements": [
            {
                "requirement_id": str(uuid4()),
                "canonical_url": "https://example.com/canonical",
                "reason": "required_exact_source_not_acquired",
            }
        ],
    }
    events = [
        {
            "sequence_number": 1,
            "event_type": "evidence.exact_source_coverage_gap",
            "payload": {"exact_source_coverage_gap": gap},
        }
    ]
    reader = PostgresResumeStateReader(lambda: _EventUOW(events))
    assert reader.exact_source_coverage_gap(run_id) == gap

    events.append(
        {
            "sequence_number": 2,
            "event_type": "evidence.exact_source_coverage_resolved",
            "payload": {"kind": "exact_source_coverage_resolved"},
        }
    )
    assert reader.exact_source_coverage_gap(run_id) is None
