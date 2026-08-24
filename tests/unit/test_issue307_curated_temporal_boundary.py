"""Curated synthesis must fail closed on a typed temporal coverage gap.

Issue #307 caller audit: the supported curated boundary
(``frun synthesize`` -> CuratedRunService.synthesize ->
CuratedSynthesisService.synthesize -> EvidencePreparationService.prepare)
previously let the deliberately unrelated ``TemporalCoverageUnsatisfied``
escape uncaught, because only ``EvidencePreparationError`` was guarded and
the CLI except-list does not include the typed condition.  The typed
temporal gap must become a controlled ``CuratedSynthesisError`` at the
service boundary instead of an uncaught traceback at the CLI.
"""

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.coverage_seed_service import (
    CompleteCoverageService,
)
from firecrawl_skill.research_store.curated_synthesis_service import (
    CuratedSynthesisError,
    CuratedSynthesisService,
)
from firecrawl_skill.research_store.evidence_preparation_service import (
    EvidencePreparationError,
)
from firecrawl_skill.research_store.temporal_coverage import (
    TemporalCoverageDiagnostics,
    TemporalCoverageUnsatisfied,
)


class _GapRaisingPreparation:
    """Evidence preparation that raises only the typed temporal condition."""

    def prepare(self, **_kwargs: object) -> None:
        raise TemporalCoverageUnsatisfied(
            TemporalCoverageDiagnostics(
                basis="freshness",
                examined_passages=3,
                qualifying_passages=0,
                missing_freshness_authority=3,
            )
        )


class _GenericFailurePreparation:
    """Evidence preparation that fails for a non-temporal reason."""

    def prepare(self, **_kwargs: object) -> None:
        raise EvidencePreparationError("no question or claim")


def _service(preparation: object) -> tuple[CuratedSynthesisService, SimpleNamespace]:
    status = SimpleNamespace(
        state="coverage_review",
        id=uuid4(),
        lifecycle_revision=3,
        execution_mode="operator_led",
    )
    service = CuratedSynthesisService(
        config=SimpleNamespace(generative_model=""),
        run_service=SimpleNamespace(uow_factory=lambda: None),
        promotion_service=SimpleNamespace(
            get_active_seal=lambda run_id: SimpleNamespace(
                members={"asset-1"}, membership_sha256="sha256:x"
            )
        ),
        evidence_preparation_service=preparation,
        synthesis_service=SimpleNamespace(),
        coverage_service=cast(
            CompleteCoverageService,
            SimpleNamespace(get_current_revision=lambda run_id: 1),
        ),
    )
    service._status = lambda external_run_id: status  # type: ignore[method-assign]
    service._preflight_semantic = lambda status: {"ready": True}  # type: ignore[method-assign]
    service._research_spec = lambda run_id: {"payload": {}}  # type: ignore[method-assign]
    service._database_research_spec_id = lambda spec_record: uuid4()  # type: ignore[method-assign]
    service._coverage_items = lambda run_id, spec, execution_mode: []  # type: ignore[method-assign]
    service._sealed_assets = lambda run_id, seal: []  # type: ignore[method-assign]
    service._ensure_synthesizing = lambda external_run_id, status: status  # type: ignore[method-assign]
    service._current_packet = lambda run_id, **kwargs: (None, "prepared")  # type: ignore[method-assign]
    return service, status


def test_curated_synthesis_converts_typed_temporal_gap_to_controlled_error() -> None:
    service, _status = _service(_GapRaisingPreparation())
    with pytest.raises(
        CuratedSynthesisError, match="temporal evidence coverage unsatisfied"
    ) as excinfo:
        service.synthesize("fr_curated_temporal")
    assert isinstance(excinfo.value.__cause__, TemporalCoverageUnsatisfied)


def test_generic_evidence_failure_remains_generic_in_curated_boundary() -> None:
    service, _status = _service(_GenericFailurePreparation())
    with pytest.raises(
        CuratedSynthesisError, match="authoritative evidence preparation failed"
    ) as excinfo:
        service.synthesize("fr_curated_temporal")
    assert isinstance(excinfo.value.__cause__, EvidencePreparationError)
    assert not isinstance(excinfo.value.__cause__, TemporalCoverageUnsatisfied)
