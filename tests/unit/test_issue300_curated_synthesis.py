"""Issue #300 AC4 application-boundary regressions for curated synthesis."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.curated_synthesis_service import (
    CuratedSynthesisError,
    CuratedSynthesisService,
)


class _RunService:
    def __init__(self, status):
        self._status = status
        self.transitions = []
        self.uow_factory = lambda: None

    def status(self, *, external_id=None, run_id=None):
        del external_id, run_id
        return self._status

    def transition(self, _run_id, next_state, **command):
        self.transitions.append((next_state, command))
        self._status.state = next_state
        self._status.lifecycle_revision += 1
        return SimpleNamespace(next_state=next_state)


class _Promotion:
    def __init__(self, seal):
        self.seal = seal
        self.calls = 0

    def get_active_seal(self, _run_id):
        self.calls += 1
        return self.seal


class _EvidencePreparation:
    def __init__(self, packet_revision: int = 8):
        self.packet_revision = packet_revision
        self.calls = 0
        self.semantic = SimpleNamespace(host_artifact_supplier=None)

    def prepare(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(packet_revision=self.packet_revision)


class _Synthesis:
    def __init__(self, overall_status: str = "completed"):
        self.overall_status = overall_status
        self.calls = []

    def run_synthesis(self, **kwargs):
        self.calls.append(kwargs)
        return {"overall_status": self.overall_status, "stages": {}}


def _service(*, execution_mode: str = "autonomous_local"):
    status = SimpleNamespace(
        id=uuid4(),
        external_id="fr_issue300_curated",
        state="coverage_review",
        execution_mode=execution_mode,
        lifecycle_revision=12,
    )
    seal = SimpleNamespace(
        members=(object(),),
        chunk_ids=(uuid4(),),
        membership_sha256="a" * 64,
    )
    evidence = _EvidencePreparation()
    synthesis = _Synthesis()
    config = SimpleNamespace(
        generative_url="http://127.0.0.1:8002/v1",
        generative_model="chat",
        generative_api_key="",
    )
    service = CuratedSynthesisService(
        config=config,
        run_service=_RunService(status),
        promotion_service=_Promotion(seal),
        evidence_preparation_service=evidence,
        synthesis_service=synthesis,
        coverage_service=SimpleNamespace(),
    )
    return service, status, seal, evidence, synthesis


def _patch_authority_helpers(monkeypatch, service):
    spec_id = uuid4()
    monkeypatch.setattr(
        service,
        "_research_spec",
        lambda _run_id: {
            "id": str(spec_id),
            "payload": {"research_spec_id": str(spec_id)},
        },
    )
    monkeypatch.setattr(service, "_coverage_items", lambda *_args: [])
    monkeypatch.setattr(service, "_sealed_assets", lambda *_args: [])
    return spec_id


def test_endpoint_unavailable_fails_before_evidence_or_membership_work(
    monkeypatch,
) -> None:
    service, _status, _seal, evidence, _synthesis = _service()
    monkeypatch.setattr(
        "firecrawl_skill.research_store.curated_synthesis_service.model_gateway.probe_local",
        lambda *_args, **_kwargs: {"status": "unavailable", "error": "offline"},
    )

    with pytest.raises(CuratedSynthesisError, match="unavailable before semantic work"):
        service.synthesize("fr_issue300_curated")

    assert evidence.calls == 0
    assert service.promotion.calls == 0


def test_reusable_current_packet_skips_evidence_preparation_and_never_finishes(
    monkeypatch,
) -> None:
    service, status, seal, evidence, synthesis = _service()
    monkeypatch.setattr(
        service,
        "_preflight_semantic",
        lambda _status: {"status": "available", "authority": "local-model"},
    )
    _patch_authority_helpers(monkeypatch, service)
    monkeypatch.setattr(service, "_current_packet", lambda *_args, **_kwargs: (7, "reused"))
    monkeypatch.setattr(service, "_reset_stale_stages", lambda *_args, **_kwargs: 0)

    result = service.synthesize("fr_issue300_curated")

    assert evidence.calls == 0
    assert len(synthesis.calls) == 1
    assert synthesis.calls[0]["packet_revision"] == 7
    assert synthesis.calls[0]["allow_commercial_fallback"] is False
    assert result["evidence"]["mode"] == "reused"
    assert result["finished"] is False
    assert result["state"] == "validating"
    assert [item[0] for item in service.run_service.transitions] == [
        "synthesizing",
        "validating",
    ]
    assert result["next_action"] == (
        "frun finish fr_issue300_curated --outcome satisfied"
    )


def test_missing_or_unverified_packet_is_prepared_then_stale_stages_reset(
    monkeypatch,
) -> None:
    service, status, seal, evidence, synthesis = _service()
    monkeypatch.setattr(
        service,
        "_preflight_semantic",
        lambda _status: {"status": "available", "authority": "local-model"},
    )
    _patch_authority_helpers(monkeypatch, service)
    monkeypatch.setattr(
        service, "_current_packet", lambda *_args, **_kwargs: (None, "unverified_history")
    )
    prepared_markers = []
    monkeypatch.setattr(
        service,
        "_record_prepared_packet",
        lambda *args, **kwargs: prepared_markers.append((args, kwargs)),
    )
    reset_calls = []
    monkeypatch.setattr(
        service,
        "_reset_stale_stages",
        lambda *args, **kwargs: reset_calls.append((args, kwargs)) or 3,
    )

    result = service.synthesize("fr_issue300_curated")

    assert evidence.calls == 1
    assert prepared_markers
    assert reset_calls
    assert reset_calls[0][1]["packet_revision"] == evidence.packet_revision
    assert synthesis.calls[0]["packet_revision"] == evidence.packet_revision
    assert result["evidence"]["mode"] == "prepared"
    assert result["stale_stage_reset_count"] == 3
    assert result["state"] == "validating"


def test_failed_synthesis_reports_explicit_resume_action_without_terminalizing(
    monkeypatch,
) -> None:
    service, status, seal, _evidence, synthesis = _service()
    synthesis.overall_status = "failed"
    monkeypatch.setattr(
        service,
        "_preflight_semantic",
        lambda _status: {"status": "available", "authority": "local-model"},
    )
    _patch_authority_helpers(monkeypatch, service)
    monkeypatch.setattr(service, "_current_packet", lambda *_args, **_kwargs: (4, "reused"))
    monkeypatch.setattr(service, "_reset_stale_stages", lambda *_args, **_kwargs: 0)

    result = service.synthesize("fr_issue300_curated")

    assert result["finished"] is False
    assert result["state"] == "synthesizing"
    assert result["next_action"] == "frun synthesize fr_issue300_curated"


def test_database_research_spec_identity_is_used_not_domain_id() -> None:
    database_id = uuid4()
    domain_id = uuid4()
    assert CuratedSynthesisService._database_research_spec_id(
        {"id": str(database_id), "payload": {"research_spec_id": str(domain_id)}}
    ) == database_id


def test_validating_state_refuses_stale_evidence_rebuild(monkeypatch) -> None:
    service, status, seal, evidence, _synthesis = _service()
    status.state = "validating"
    monkeypatch.setattr(
        service,
        "_preflight_semantic",
        lambda _status: {"status": "available", "authority": "local-model"},
    )
    _patch_authority_helpers(monkeypatch, service)
    monkeypatch.setattr(
        service, "_current_packet", lambda *_args, **_kwargs: (None, "stale_spec")
    )

    with pytest.raises(CuratedSynthesisError, match="reopen"):
        service.synthesize("fr_issue300_curated")

    assert evidence.calls == 0
    assert service.run_service.transitions == []


def test_curated_synthesis_bypasses_cross_run_semantic_cache() -> None:
    from firecrawl_skill.research_store.curated_synthesis_service import (
        AuthorityAlignedLocalSynthesisService,
    )

    service = object.__new__(AuthorityAlignedLocalSynthesisService)
    assert service._check_cache(stage="draft") is None
    assert service._write_cache(stage="draft") is None


def test_agent_led_synthesis_does_not_require_local_generative_governor() -> None:
    from firecrawl_skill.research_store.curated_synthesis_service import (
        AuthorityAlignedLocalSynthesisService,
    )

    class _Governor:
        def acquire_sync(self, *_args, **_kwargs):
            raise AssertionError("agent_led must not acquire local generative resource")

        def release_sync(self, *_args, **_kwargs):
            raise AssertionError("agent_led must not release unacquired local resource")

    service = object.__new__(AuthorityAlignedLocalSynthesisService)
    service._resource_governor = _Governor()
    token = service._active_execution_mode.set("agent_led")
    try:
        assert service._bounded_llm_call(lambda: "host-artifact") == "host-artifact"
    finally:
        service._active_execution_mode.reset(token)


@pytest.mark.parametrize(
    ("ready", "expected"),
    [
        (
            True,
            "frun finish fr_issue300_curated --outcome satisfied",
        ),
        (False, "frun synthesize fr_issue300_curated"),
    ],
)
def test_validating_resume_guidance_uses_current_authoritative_gates(
    monkeypatch, ready: bool, expected: str
) -> None:
    from firecrawl_skill.research_store.curated_run_service import CuratedRunService

    class _ModeStatus:
        run_mode = "curated"
        run = SimpleNamespace(id=uuid4(), state="validating")

        def to_dict(self):
            return {"run_mode": self.run_mode, "state": self.run.state}

    service = object.__new__(CuratedRunService)
    monkeypatch.setattr(service, "status", lambda _external_id: _ModeStatus())
    monkeypatch.setattr(service, "_satisfied_finish_ready", lambda _run_id: ready)

    result = service.resume("fr_issue300_curated")
    assert result["next_action"] == expected
