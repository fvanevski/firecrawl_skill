"""Tests for agent-led handoff payload and builder (Phase 7, issue #62).

Covers:
- HandoffPayload dataclass validation (schema version, revisions, serialization)
- Host-agent fixture tests (payload is self-contained, no scratch files needed)
- No-inner-call tests (builder does not trigger semantic calls)
- Token-bound tests (token_limits are included and bounded)
- Citation resolution (all citation-ready passage IDs exist in packet)
- Limitations and unresolved items are explicit
- Optional outline generation
- HandoffBuilder failure paths (missing packet, missing spec)
- HandoffPayload.to_dict() round-trip
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_domain.models import HandoffPayload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec_payload() -> dict[str, Any]:
    return {
        "schema_version": "research-spec-v1",
        "research_spec_id": str(uuid4()),
        "objective": "Test objective",
        "research_archetype": "academic",
        "risk_level": "low",
        "execution_mode": "agent_led",
        "questions": [
            {
                "question_id": str(uuid4()),
                "question_text": "What is X?",
                "answer": "",
            }
        ],
        "claims_to_validate": [],
        "entities": [],
        "jurisdictions": [],
        "time_window": {"start": "2025-01-01", "end": "2026-12-31"},
        "freshness_requirements": [],
        "required_source_classes": [],
        "corroboration_requirements": [],
        "contradiction_requirements": [],
        "excluded_interpretations": [],
        "structured_data_requirements": [],
        "completion_criteria": [
            {
                "requirement_id": str(uuid4()),
                "description": "At least one source",
            }
        ],
        "user_constraints": [],
        "ambiguities": [],
        "assumptions": [],
    }


def _make_ledger_payload() -> dict[str, Any]:
    return {
        "schema_version": "coverage-ledger-v1",
        "run_id": str(uuid4()),
        "coverage_revision": 1,
        "total_items": 3,
        "status_counts": {"covered": 2, "uncovered": 1},
        "type_counts": {"question": 2, "claim": 1},
        "overall_status": "partial",
    }


def _make_packet_payload() -> dict[str, Any]:
    claim_id = uuid4()
    passage_id = uuid4()
    return {
        "schema_version": "evidence-packet-v1",
        "run_id": str(uuid4()),
        "research_spec_id": str(uuid4()),
        "coverage_revision": 1,
        "claims": [
            {
                "claim_id": str(claim_id),
                "statement": "X is Y",
                "semantic_status": "supported",
                "uncertainty": "low",
            }
        ],
        "passages": [
            {
                "passage_id": str(passage_id),
                "candidate_id": str(uuid4()),
                "snapshot_id": str(uuid4()),
                "chunk_id": str(uuid4()),
                "text": "Some excerpt",
                "source_url": "https://example.com",
            }
        ],
        "omitted_passages": [],
        "claim_evidence_bindings": [
            {
                "binding_id": str(uuid4()),
                "claim_id": str(claim_id),
                "passage_ids": [str(passage_id)],
                "relationship": "supports",
                "confidence": 0.9,
                "uncertainty": "low",
                "model": "local-model",
                "prompt_version": "v1",
                "schema_version": 1,
                "input_packet_revision": 1,
            }
        ],
        "corroborating_groups": [],
        "contradicting_groups": [],
        "qualifying_groups": [],
        "near_duplicate_groups": [],
        "source_diversity_summary": {"domains": 1},
        "freshness_summary": {"fresh": 1},
        "limitations": ["Limited source diversity", "Single domain coverage"],
        "unresolved_items": [str(uuid4())],
        "independence_assessments": [],
        "retrieval_provenance": [],
    }


def _make_handoff_payload(**overrides) -> HandoffPayload:
    """Build a minimal valid HandoffPayload with optional overrides."""
    base = {
        "schema_version": HandoffPayload.SCHEMA_VERSION,
        "run_id": uuid4(),
        "research_spec": _make_spec_payload(),
        "coverage_ledger": _make_ledger_payload(),
        "evidence_packet": _make_packet_payload(),
        "evidence_packet_revision": 1,
        "coverage_revision": 1,
        "limitations": ("Limited sources",),
        "unresolved_items": (uuid4(),),
        "outline": ("1. Evidence summary", "2. Supported findings"),
        "citation_ready": {"claims": [], "passages": [], "bindings": {}},
        "token_limits": {"max_input_tokens": 8192, "max_output_tokens": 4096},
        "created_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return HandoffPayload(**base)


class _HandoffMockUow:
    """Minimal role-explicit UoW test double used by HandoffBuilder tests."""

    def __init__(
        self,
        *,
        evidence_packet: object | None,
        research_spec: dict[str, Any] | None,
        coverage_summary: dict[str, Any] | None,
        coverage_revision: int = 1,
        coverage_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._evidence_packet = evidence_packet
        self._research_spec = research_spec
        self._coverage_summary = coverage_summary
        self._coverage_revision = coverage_revision
        self._coverage_events = coverage_events or []

        # Explicit named repository roles. One compact test double implements
        # the narrow protocols; callers still access them through role names.
        self.evidence_packets = self
        self.runs = self
        self.coverage = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_evidence_packet(self, run_id):
        return self._evidence_packet

    def get_research_spec(self, run_id):
        return self._research_spec

    def get_coverage_summary(self, run_id):
        return self._coverage_summary

    def get_current_revision(self, run_id):
        return self._coverage_revision

    def list_coverage_events(self, run_id, limit=100, offset=0):
        return self._coverage_events[:limit]


def _packet_record(run_id: UUID, *, packet_revision: int = 1):
    class MockRecord:
        coverage_revision = 1

        def __init__(self):
            self.packet_revision = packet_revision

        def to_dict(self):
            return {
                "id": str(uuid4()),
                "run_id": str(run_id),
                "research_spec_id": str(uuid4()),
                "coverage_revision": 1,
                "packet_revision": packet_revision,
                "payload": _make_packet_payload(),
                "created_at": datetime.now(timezone.utc),
            }

    return MockRecord()


# ---------------------------------------------------------------------------
# HandoffPayload dataclass tests
# ---------------------------------------------------------------------------


class TestHandoffPayloadSchema:
    """Tests for the HandoffPayload dataclass."""

    def test_schema_version_constant(self):
        assert HandoffPayload.SCHEMA_VERSION == "handoff-payload-v1"

    def test_valid_payload(self):
        payload = _make_handoff_payload()
        assert payload.schema_version == "handoff-payload-v1"
        assert isinstance(payload.run_id, UUID)
        assert isinstance(payload.research_spec, dict)
        assert isinstance(payload.coverage_ledger, dict)
        assert isinstance(payload.evidence_packet, dict)
        assert payload.evidence_packet_revision >= 1
        assert payload.coverage_revision >= 1
        assert isinstance(payload.limitations, tuple)
        assert isinstance(payload.unresolved_items, tuple)
        assert payload.outline is not None
        assert isinstance(payload.citation_ready, dict)
        assert isinstance(payload.token_limits, dict)
        assert isinstance(payload.created_at, datetime)

    def test_null_outline_allowed(self):
        payload = _make_handoff_payload(outline=None)
        assert payload.outline is None

    def test_null_token_limits_allowed(self):
        payload = _make_handoff_payload(token_limits=None)
        assert payload.token_limits is None

    def test_empty_limitations_allowed(self):
        payload = _make_handoff_payload(limitations=())
        assert payload.limitations == ()

    def test_empty_unresolved_items_allowed(self):
        payload = _make_handoff_payload(unresolved_items=())
        assert payload.unresolved_items == ()

    def test_rejects_bad_schema_version(self):
        with pytest.raises(ValueError, match="unsupported schema_version"):
            _make_handoff_payload(schema_version="handoff-payload-v0")

    def test_rejects_negative_packet_revision(self):
        with pytest.raises(ValueError, match="evidence_packet_revision must be >= 0"):
            _make_handoff_payload(evidence_packet_revision=-1)

    def test_rejects_negative_coverage_revision(self):
        with pytest.raises(ValueError, match="coverage_revision must be >= 0"):
            _make_handoff_payload(coverage_revision=-1)

    def test_zero_packet_revision_allowed_for_degraded(self):
        payload = _make_handoff_payload(evidence_packet_revision=0)
        assert payload.evidence_packet_revision == 0

    def test_zero_coverage_revision_allowed_for_degraded(self):
        payload = _make_handoff_payload(coverage_revision=0)
        assert payload.coverage_revision == 0

    def test_to_dict_round_trip(self):
        payload = _make_handoff_payload()
        d = payload.to_dict()
        assert d["schema_version"] == "handoff-payload-v1"
        assert isinstance(d["run_id"], str)
        assert isinstance(d["research_spec"], dict)
        assert isinstance(d["coverage_ledger"], dict)
        assert isinstance(d["evidence_packet"], dict)
        assert isinstance(d["limitations"], list)
        assert isinstance(d["unresolved_items"], list)
        assert isinstance(d["created_at"], str)
        assert d["outline"] is not None
        assert isinstance(d["outline"], list)
        assert d["token_limits"] is not None
        assert isinstance(d["token_limits"], dict)

    def test_to_dict_null_outline(self):
        payload = _make_handoff_payload(outline=None)
        assert payload.to_dict()["outline"] is None

    def test_to_dict_null_token_limits(self):
        payload = _make_handoff_payload(token_limits=None)
        assert payload.to_dict()["token_limits"] is None

    def test_json_serializable(self):
        json.dumps(_make_handoff_payload().to_dict(), default=str)

    def test_frozen_cannot_mutate(self):
        payload = _make_handoff_payload()
        with pytest.raises(AttributeError):
            cast(Any, payload).run_id = uuid4()


# ---------------------------------------------------------------------------
# Host-agent fixture tests
# ---------------------------------------------------------------------------


class TestHostAgentFixture:
    def test_payload_contains_spec(self):
        spec = _make_handoff_payload().research_spec
        assert "objective" in spec
        assert "questions" in spec
        assert "completion_criteria" in spec
        assert "research_archetype" in spec

    def test_payload_contains_ledger(self):
        ledger = _make_handoff_payload().coverage_ledger
        assert "coverage_revision" in ledger
        assert "overall_status" in ledger
        assert "status_counts" in ledger

    def test_payload_contains_packet(self):
        packet = _make_handoff_payload().evidence_packet
        assert "claims" in packet
        assert "passages" in packet
        assert "claim_evidence_bindings" in packet
        assert "limitations" in packet
        assert "unresolved_items" in packet

    def test_no_filesystem_paths_as_authoritative(self):
        payload = _make_handoff_payload()
        for text in [
            json.dumps(payload.research_spec),
            json.dumps(payload.evidence_packet),
            json.dumps(payload.coverage_ledger),
        ]:
            assert "/tmp/" not in text
            assert "scratch" not in text.lower()

    def test_citations_resolve_to_packet(self):
        packet = _make_packet_payload()
        passage_ids = {p["passage_id"] for p in packet["passages"]}
        binding_passage_ids = set()
        for binding in packet["claim_evidence_bindings"]:
            binding_passage_ids.update(binding["passage_ids"])
        assert binding_passage_ids.issubset(passage_ids)


class TestNoInnerCalls:
    def test_payload_construction_is_deterministic(self):
        d1 = _make_handoff_payload().to_dict()
        d2 = _make_handoff_payload().to_dict()
        assert d1["schema_version"] == d2["schema_version"]
        assert d1["evidence_packet_revision"] == d2["evidence_packet_revision"]
        assert d1["coverage_revision"] == d2["coverage_revision"]

    def test_no_model_calls_in_to_dict(self):
        result = _make_handoff_payload().to_dict()
        assert isinstance(result, dict)
        assert "schema_version" in result


class TestTokenBounds:
    def test_token_limits_included(self):
        payload = _make_handoff_payload()
        assert payload.token_limits is not None
        assert "max_input_tokens" in payload.token_limits
        assert "max_output_tokens" in payload.token_limits

    def test_token_limits_bounded(self):
        payload = _make_handoff_payload()
        assert payload.token_limits is not None
        for value in payload.token_limits.values():
            assert isinstance(value, int)
            assert value > 0

    def test_no_token_limits_when_none(self):
        assert _make_handoff_payload(token_limits=None).token_limits is None

    def test_token_limits_serialized(self):
        payload = _make_handoff_payload()
        assert payload.to_dict()["token_limits"] == payload.token_limits

    def test_custom_token_limits(self):
        custom = {"max_input_tokens": 4096, "max_output_tokens": 2048}
        assert _make_handoff_payload(token_limits=custom).token_limits == custom


class TestCitationResolution:
    def test_citation_ready_contains_claims(self):
        assert "claims" in _make_handoff_payload().citation_ready

    def test_citation_ready_contains_passages(self):
        assert "passages" in _make_handoff_payload().citation_ready

    def test_citation_ready_contains_bindings(self):
        assert "bindings" in _make_handoff_payload().citation_ready

    def test_unresolved_items_explicit(self):
        item_id = uuid4()
        assert item_id in _make_handoff_payload(unresolved_items=(item_id,)).unresolved_items

    def test_limitations_explicit(self):
        limitations = ("Single source", "Outdated data", "Limited domain")
        payload = _make_handoff_payload(limitations=limitations)
        assert payload.limitations == limitations
        assert len(payload.limitations) == 3


class TestOutlineGeneration:
    def test_outline_with_claims(self):
        payload = _make_handoff_payload()
        assert payload.outline is not None
        assert len(payload.outline) > 0

    def test_outline_can_be_none(self):
        packet = _make_packet_payload()
        packet["claims"] = []
        assert _make_handoff_payload(evidence_packet=packet, outline=None).outline is None

    def test_outline_is_tuple(self):
        payload = _make_handoff_payload()
        assert isinstance(payload.outline, tuple)
        assert all(isinstance(item, str) for item in payload.outline)


# ---------------------------------------------------------------------------
# HandoffBuilder failure and success paths
# ---------------------------------------------------------------------------


class TestHandoffBuilderFailurePaths:
    def test_missing_evidence_packet_produces_degraded_payload(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        uow = _HandoffMockUow(
            evidence_packet=None,
            research_spec=_make_spec_payload(),
            coverage_summary=_make_ledger_payload(),
        )
        payload = HandoffBuilder(lambda: uow).build(run_id)

        assert isinstance(payload, HandoffPayload)
        assert payload.evidence_packet.get("degraded") is True
        assert payload.evidence_packet.get("reason") == "evidence_packet_missing"
        assert payload.evidence_packet_revision == 0
        assert any("Evidence packet is missing" in l for l in payload.limitations)
        assert payload.citation_ready["metadata"]["degraded"] is True
        assert payload.citation_ready["metadata"]["reason"] == "evidence_packet_missing"
        assert payload.outline is None

    def test_missing_research_spec_produces_degraded_payload(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id),
            research_spec=None,
            coverage_summary=_make_ledger_payload(),
        )
        payload = HandoffBuilder(lambda: uow).build(run_id)
        assert payload.research_spec == {}
        assert any("ResearchSpec is missing" in l for l in payload.limitations)

    def test_both_packet_and_spec_missing(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        uow = _HandoffMockUow(
            evidence_packet=None,
            research_spec=None,
            coverage_summary=_make_ledger_payload(),
        )
        payload = HandoffBuilder(lambda: uow).build(run_id)
        assert payload.evidence_packet.get("degraded") is True
        assert payload.research_spec == {}
        limitation_text = " ".join(payload.limitations)
        assert "Evidence packet is missing" in limitation_text
        assert "ResearchSpec is missing" in limitation_text


class TestHandoffBuilderSuccess:
    def test_build_returns_valid_payload(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id, packet_revision=2),
            research_spec={
                "id": str(uuid4()),
                "run_id": str(run_id),
                "spec_revision": 1,
                "payload": _make_spec_payload(),
            },
            coverage_summary=_make_ledger_payload(),
        )
        payload = HandoffBuilder(
            lambda: uow,
            token_limits={"max_input_tokens": 8192},
            max_passages=64,
            max_claims=32,
        ).build(run_id)

        assert payload.schema_version == "handoff-payload-v1"
        assert payload.run_id == run_id
        assert payload.evidence_packet_revision == 2
        assert payload.token_limits == {"max_input_tokens": 8192}
        assert payload.limitations == (
            "Limited source diversity",
            "Single domain coverage",
        )
        assert len(payload.unresolved_items) == 1

    def test_coverage_rebuild_degraded_when_truncated(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        events = [
            {
                "coverage_item_id": str(uuid4()),
                "item_type": "question",
                "status": "satisfied",
                "freshness_status": "satisfied",
            }
            for _ in range(100_000)
        ]
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id),
            research_spec=_make_spec_payload(),
            coverage_summary=None,
            coverage_events=events,
        )
        payload = HandoffBuilder(lambda: uow).build(run_id)
        assert payload.coverage_ledger.get("_degraded") is True
        assert "event list truncated" in payload.coverage_ledger.get(
            "_degradation_reason", ""
        )
        assert "Coverage summary was rebuilt from events" in " ".join(
            payload.limitations
        )

    def test_coverage_rebuild_not_degraded_when_under_limit(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        events = [
            {
                "coverage_item_id": str(uuid4()),
                "item_type": "question",
                "status": "satisfied",
                "freshness_status": "satisfied",
            }
            for _ in range(50)
        ]
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id),
            research_spec=_make_spec_payload(),
            coverage_summary=None,
            coverage_events=events,
        )
        payload = HandoffBuilder(lambda: uow).build(run_id)
        assert "_degraded" not in payload.coverage_ledger
        assert all("Coverage summary was rebuilt" not in l for l in payload.limitations)


# ---------------------------------------------------------------------------
# Integration-style: CLI argument parsing
# ---------------------------------------------------------------------------


class TestCLIArgumentParsing:
    def test_handoff_subcommand_exists(self):
        from firecrawl_skill.research_store.cli import parser

        assert parser() is not None

    def test_handoff_requires_run_id(self):
        from firecrawl_skill.research_store.cli import parser

        args = parser().parse_args(["handoff", str(uuid4())])
        assert args.command == "handoff"
        assert args.run_id is not None

    def test_handoff_optional_args(self):
        from firecrawl_skill.research_store.cli import parser

        args = parser().parse_args(
            [
                "handoff",
                str(uuid4()),
                "--max-passages",
                "64",
                "--max-claims",
                "32",
                "--token-limit-max-input",
                "4096",
                "--output",
                "/tmp/handoff.json",
            ]
        )
        assert args.command == "handoff"
        assert args.max_passages == 64
        assert args.max_claims == 32
        assert args.token_limit_max_input == 4096
        assert args.output == "/tmp/handoff.json"


class TestHandoffPayloadImport:
    def test_import_from_models(self):
        from firecrawl_skill.research_domain.models import HandoffPayload

        assert HandoffPayload.SCHEMA_VERSION == "handoff-payload-v1"

    def test_import_from_domain_init(self):
        from firecrawl_skill.research_domain import HandoffPayload

        assert HandoffPayload.SCHEMA_VERSION == "handoff-payload-v1"

    def test_import_from_handoff_module(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        assert HandoffBuilder is not None

    def test_handoff_payload_in_canonical_models(self):
        from firecrawl_skill.research_domain.models import CANONICAL_MODELS, HandoffPayload

        assert HandoffPayload in CANONICAL_MODELS


class TestCLITokenLimitPropagation:
    def test_token_limits_propagate_through_builder(self):
        from firecrawl_skill.research_store.handoff import HandoffBuilder

        run_id = uuid4()
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id),
            research_spec=_make_spec_payload(),
            coverage_summary=_make_ledger_payload(),
        )
        payload = HandoffBuilder(
            lambda: uow,
            token_limits={"max_input_tokens": 4096, "max_output_tokens": 2048},
        ).build(run_id)
        assert payload.token_limits == {
            "max_input_tokens": 4096,
            "max_output_tokens": 2048,
        }


# ---------------------------------------------------------------------------
# CLI end-to-end: handoff command execution path
# ---------------------------------------------------------------------------


class TestCLIHandoffExecution:
    @staticmethod
    def _uow_factory(run_id: UUID):
        uow = _HandoffMockUow(
            evidence_packet=_packet_record(run_id),
            research_spec=_make_spec_payload(),
            coverage_summary=_make_ledger_payload(),
        )
        return lambda *args, **kwargs: uow

    def test_handoff_command_produces_valid_json(self, tmp_path, monkeypatch):
        from firecrawl_skill.research_store.cli import main

        run_id = uuid4()
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        import firecrawl_skill.research_store.cli as cli_mod

        monkeypatch.setattr(cli_mod, "PostgresUnitOfWork", self._uow_factory(run_id))
        output_file = tmp_path / "handoff.json"
        result = main(["handoff", str(run_id), "--output", str(output_file)])

        assert result == {"exported_to": str(output_file)}
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["schema_version"] == "handoff-payload-v1"
        assert data["run_id"] == str(run_id)
        assert "evidence_packet" in data
        assert "coverage_ledger" in data
        assert "citation_ready" in data
        assert "total_items" in data["coverage_ledger"]

    def test_handoff_stdout_produces_valid_json(self, monkeypatch):
        from contextlib import redirect_stdout
        from io import StringIO

        from firecrawl_skill.research_store.cli import main

        run_id = uuid4()
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        import firecrawl_skill.research_store.cli as cli_mod

        monkeypatch.setattr(cli_mod, "PostgresUnitOfWork", self._uow_factory(run_id))
        f = StringIO()
        with redirect_stdout(f):
            result = main(["handoff", str(run_id)])

        assert result == {}
        data = json.loads(f.getvalue())
        assert data["schema_version"] == "handoff-payload-v1"
        assert data["run_id"] == str(run_id)
        assert "total_items" in data["coverage_ledger"]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_schema_matches_dataclass_fields(self):
        import json as _json

        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "research-workflow"
            / "handoff-payload-v1.json"
        )
        with open(schema_path) as f:
            schema = _json.load(f)

        schema_props = set(schema["properties"].keys())
        dataclass_fields = {f.name for f in HandoffPayload.__dataclass_fields__.values()}
        assert schema_props == dataclass_fields, (
            f"Schema props {schema_props} != dataclass fields {dataclass_fields}"
        )

    def test_fixture_validates_against_schema(self):
        import json as _json

        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        fixture_path = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "fixtures"
            / "research_domain"
            / "valid.json"
        )
        with open(fixture_path) as f:
            fixtures = _json.load(f)

        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "research-workflow"
            / "handoff-payload-v1.json"
        )
        with open(schema_path) as f:
            schema = _json.load(f)

        jsonschema.validate(fixtures["handoff-payload-v1"], schema)
        assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
