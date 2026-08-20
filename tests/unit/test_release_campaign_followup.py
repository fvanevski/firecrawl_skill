"""Focused regressions for failures exposed by Real campaign 31430688783."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import MethodType
from uuid import UUID, uuid4

import model_gateway
from model_gateway import StructuredResult

from firecrawl_skill.research_store import authorized_semantic, lifecycle_guard
from firecrawl_skill.research_store.authorized_semantic import (
    call_authorized_structured,
)
from firecrawl_skill.research_store.lifecycle_guard import GuardedResearchRunService


@contextmanager
def _status_uow(execution_mode: str = "autonomous_local"):
    class Runs:
        @staticmethod
        def get_run_status(*, run_id):
            del run_id
            return {"execution_mode": execution_mode, "lifecycle_revision": 7}

    class Uow:
        runs = Runs()

    yield Uow()


class _SemanticRecorder:
    def __init__(self) -> None:
        self.started_schema = None
        self.finished_artifacts: list | None = None
        self.finished_status = None
        self.finished_provenance: dict | None = None
        self.host_artifact_supplier = None

    def uow_factory(self):
        return _status_uow()

    def start_model_call(self, context, **kwargs):
        del context
        self.started_schema = kwargs["schema"]
        return uuid4()

    def finish_model_call(
        self,
        context,
        call_id,
        *,
        status,
        provenance,
        attempts,
        artifacts,
        error="",
    ):
        del context, call_id, attempts, error
        self.finished_status = status
        self.finished_provenance = dict(provenance)
        self.finished_artifacts = list(artifacts)
        return tuple(uuid4() for _ in artifacts)


def _citation_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "schema_version",
            "run_id",
            "evidence_packet_revision",
            "draft_revision",
            "pass_status",
            "validation_results",
            "invented_citations",
            "unsupported_claims",
            "entailment_mismatches",
        ],
        "properties": {
            "schema_version": {"type": "string"},
            "run_id": {"type": "string"},
            "evidence_packet_revision": {"type": "integer"},
            "draft_revision": {"type": "integer"},
            "pass_status": {"type": "string"},
            "validation_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "section_id",
                        "claim_id",
                        "passage_ids",
                        "status",
                        "issue",
                    ],
                    "properties": {
                        "section_id": {"type": "string"},
                        "claim_id": {"type": "string"},
                        "passage_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "status": {
                            "type": "string",
                            "enum": [
                                "valid",
                                "invalid",
                                "unsupported",
                                "entailment_mismatch",
                            ],
                        },
                        "issue": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "invented_citations": {"type": "array", "items": {"type": "object"}},
            "unsupported_claims": {"type": "array", "items": {"type": "object"}},
            "entailment_mismatches": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
        "additionalProperties": False,
    }


def _citation_fixture(run_id: UUID) -> dict:
    return {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 3,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "section-1",
                "claim_id": "claim-1",
                "passage_ids": ["passage-1", "passage-2"],
                "status": "valid",
                "issue": "",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }


def test_json_decode_length_retry_expands_output_budget(monkeypatch):
    """Malformed JSON caused by finish_reason=length must grow the next budget."""
    monkeypatch.setattr(
        model_gateway,
        "probe_local",
        lambda *_args, **_kwargs: {"status": "available"},
    )
    budgets = []
    responses = iter(
        [
            (
                {
                    "id": "attempt-1",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"ok":'},
                        }
                    ],
                    "usage": {"completion_tokens": 8},
                },
                "req-1",
                200,
            ),
            (
                {
                    "id": "attempt-2",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"ok": True})},
                        }
                    ],
                    "usage": {"completion_tokens": 3},
                },
                "req-2",
                200,
            ),
        ]
    )

    def fake_request(_url, payload, _headers, _timeout):
        budgets.append(payload["max_tokens"])
        return next(responses)

    monkeypatch.setattr(model_gateway, "_request_json", fake_request)
    result = model_gateway.call_structured(
        "local",
        "chat",
        "system",
        "user",
        {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        },
        max_output_tokens=8,
        max_attempts=2,
        expand_output_on_length=True,
    )

    assert result.error == ""
    assert result.value == {"ok": True}
    assert budgets == [8, 16]
    assert result.attempts[0]["finish_reason"] == "length"


def test_claim_binding_forces_length_expansion(monkeypatch):
    """The claim-binding authority path must opt into length recovery."""
    service = _SemanticRecorder()
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return StructuredResult({"evaluations": []}, {}, ())

    monkeypatch.setattr(authorized_semantic.model_gateway, "call_structured", fake_call)
    run_id = uuid4()
    result = call_authorized_structured(
        semantic_service=service,
        semantic_context={
            "run_id": str(run_id),
            "run_revision": 7,
            "stage": "claim_binding",
            "schema_name": "claim-binding-v1",
            "schema_version": 1,
            "idempotency_key": "binding-test",
        },
        deterministic_fixture={"evaluations": []},
        actor_identifier="test",
        provider="local",
        model="chat",
        schema={"type": "object"},
        system_prompt="system",
        user_prompt="user",
        max_output_tokens=1024,
        expand_output_on_length=False,
        prompt_version="test-v1",
    )

    assert result.error == ""
    assert captured["expand_output_on_length"] is True


def test_citation_model_supplies_verdicts_but_persisted_artifact_has_exact_ids(
    monkeypatch,
):
    """Immutable citation identity is deterministic, not model-authored."""
    service = _SemanticRecorder()
    run_id = uuid4()
    fixture = _citation_fixture(run_id)
    full_schema = _citation_schema()
    observed_model_schema = {}

    def fake_call(**kwargs):
        observed_model_schema.update(kwargs["schema"])
        persistence = kwargs["semantic_persistence"]
        context = kwargs["semantic_context"]
        call_id = persistence.start_model_call(
            context,
            provider="local",
            requested_model="chat",
            model_revision="",
            endpoint_alias="local",
            prompt_version="citation-v1",
            prompt_hash="hash",
            schema=kwargs["schema"],
            input_token_estimate=10,
        )
        verdict = {"validation_results": [{"status": "valid", "issue": ""}]}
        kwargs["post_validate"](verdict)
        artifact_ids = persistence.finish_model_call(
            context,
            call_id,
            status="complete",
            provenance={"provider": "local"},
            attempts=[],
            artifacts=[
                {
                    "attempt": 1,
                    "payload": verdict,
                    "validation_errors": [],
                }
            ],
        )
        return StructuredResult(
            verdict,
            {"provider": "local"},
            (),
            semantic_call_id=call_id,
            artifact_ids=artifact_ids,
        )

    monkeypatch.setattr(authorized_semantic.model_gateway, "call_structured", fake_call)
    result = call_authorized_structured(
        semantic_service=service,
        semantic_context={
            "run_id": str(run_id),
            "run_revision": 7,
            "stage": "citation_pass",
            "schema_name": "synthesis-citation-pass-v1",
            "schema_version": 1,
            "idempotency_key": "citation-test",
        },
        deterministic_fixture=fixture,
        actor_identifier="test",
        provider="local",
        model="chat",
        schema=full_schema,
        system_prompt="validate citations",
        user_prompt="draft references",
        prompt_version="citation-v1",
    )

    assert result.error == ""
    assert result.value == fixture
    assert result.provenance["citation_identity_binding"] == "deterministic"
    verdict_item = observed_model_schema["properties"]["validation_results"]["items"]
    assert set(verdict_item["properties"]) == {"status", "issue"}
    assert service.finished_status == "complete"
    assert service.finished_provenance is not None
    assert service.finished_provenance["citation_identity_binding"] == "deterministic"
    assert service.finished_artifacts is not None
    persisted = service.finished_artifacts[-1]["payload"]
    assert persisted == fixture
    assert persisted["validation_results"][0]["section_id"] == "section-1"
    assert persisted["validation_results"][0]["claim_id"] == "claim-1"
    assert persisted["validation_results"][0]["passage_ids"] == [
        "passage-1",
        "passage-2",
    ]


class _PreparedProvenance:
    def completion_fields(self):
        return {
            "source_manifest_sha256": "a" * 64,
            "answer_sha256": "b" * 64,
            "provenance_type": "authoritative",
            "completion_provenance": {"schema_version": "completion-v1"},
        }


@contextmanager
def _empty_uow():
    yield object()


def _guard_with_captured_commit():
    service = object.__new__(GuardedResearchRunService)
    service.uow_factory = _empty_uow
    service.policy_version = "run-state-v1"
    captured = {}

    def resolve_census(_self, _run_id, supplied, *, missing_reason):
        del supplied, missing_reason
        return {"schema_version": "terminal-state-census-v1", "available": True}

    def commit(_self, run_id, **kwargs):
        captured["run_id"] = run_id
        captured.update(kwargs)
        return {
            "transition_id": uuid4(),
            "event_id": uuid4(),
            "prior_state": "validating",
            "next_state": "completed",
            "lifecycle_revision": kwargs["expected_revision"] + 1,
            "reused": False,
            "terminal_decision_id": uuid4(),
        }

    service._resolve_state_census = MethodType(resolve_census, service)
    service.commit_terminal_decision = MethodType(commit, service)
    return service, captured


def test_terminal_stage_completion_hydrates_authoritative_provenance(monkeypatch):
    """Only the exact orchestrator TerminalStage empty completion is hydrated."""
    service, captured = _guard_with_captured_commit()
    calls = []

    def load(_uow, run_id, *, for_update):
        calls.append((run_id, for_update))
        return _PreparedProvenance()

    monkeypatch.setattr(
        lifecycle_guard, "load_authoritative_completion_provenance", load
    )
    run_id = uuid4()
    result = service.complete(
        run_id,
        expected_revision=11,
        idempotency_key="terminal-stage-test",
        actor_type="orchestrator",
        actor_identifier="TerminalStage",
        reason="sufficient coverage",
        outcome="completed",
    )

    assert result.next_state == "completed"
    assert calls == [(run_id, False)]
    assert captured["completion"]["source_manifest_sha256"] == "a" * 64
    assert captured["completion"]["answer_sha256"] == "b" * 64
    assert captured["completion"]["provenance_type"] == "authoritative"
    assert captured["completion"]["completion_provenance"] == {
        "schema_version": "completion-v1"
    }


def test_other_completed_callers_are_not_auto_hydrated(monkeypatch):
    """CLI/service callers must continue to provide completion assertions."""
    service, captured = _guard_with_captured_commit()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("non-TerminalStage caller must not auto-load provenance")

    monkeypatch.setattr(
        lifecycle_guard,
        "load_authoritative_completion_provenance",
        unexpected,
    )
    run_id = uuid4()
    service.complete(
        run_id,
        expected_revision=4,
        idempotency_key="explicit-caller-test",
        actor_type="cli",
        actor_identifier="research-db",
        reason="operator completion",
        outcome="completed",
        completion={
            "source_manifest_sha256": "c" * 64,
            "answer_sha256": "d" * 64,
            "provenance_type": "authoritative",
            "completion_provenance": {"schema_version": "completion-v1"},
        },
    )

    assert captured["completion"]["source_manifest_sha256"] == "c" * 64
    assert captured["completion"]["answer_sha256"] == "d" * 64
