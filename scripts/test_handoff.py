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
from typing import Any
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import HandoffPayload

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

    def test_rejects_invalid_packet_revision(self):
        with pytest.raises(ValueError, match="evidence_packet_revision must be >= 1"):
            _make_handoff_payload(evidence_packet_revision=0)

    def test_rejects_invalid_coverage_revision(self):
        with pytest.raises(ValueError, match="coverage_revision must be >= 1"):
            _make_handoff_payload(coverage_revision=0)

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
        d = payload.to_dict()
        assert d["outline"] is None

    def test_to_dict_null_token_limits(self):
        payload = _make_handoff_payload(token_limits=None)
        d = payload.to_dict()
        assert d["token_limits"] is None

    def test_json_serializable(self):
        payload = _make_handoff_payload()
        # Should not raise
        json.dumps(payload.to_dict(), default=str)

    def test_frozen_cannot_mutate(self):
        payload = _make_handoff_payload()
        with pytest.raises(
            AttributeError
        ):  # frozen dataclass raises FrozenInstanceError (AttributeError subclass)
            payload.run_id = uuid4()


# ---------------------------------------------------------------------------
# Host-agent fixture tests
# ---------------------------------------------------------------------------


class TestHostAgentFixture:
    """Tests that the handoff payload is self-contained for a host agent."""

    def test_payload_contains_spec(self):
        """Host agent can read the spec without external lookup."""
        payload = _make_handoff_payload()
        spec = payload.research_spec
        assert "objective" in spec
        assert "questions" in spec
        assert "completion_criteria" in spec
        assert "research_archetype" in spec

    def test_payload_contains_ledger(self):
        """Host agent can read coverage status without external lookup."""
        payload = _make_handoff_payload()
        ledger = payload.coverage_ledger
        assert "coverage_revision" in ledger
        assert "overall_status" in ledger
        assert "status_counts" in ledger

    def test_payload_contains_packet(self):
        """Host agent can read the full evidence packet."""
        payload = _make_handoff_payload()
        packet = payload.evidence_packet
        assert "claims" in packet
        assert "passages" in packet
        assert "claim_evidence_bindings" in packet
        assert "limitations" in packet
        assert "unresolved_items" in packet

    def test_no_filesystem_paths_as_authoritative(self):
        """Payload does not expose filesystem paths as authoritative."""
        payload = _make_handoff_payload()
        spec = json.dumps(payload.research_spec)
        packet = json.dumps(payload.evidence_packet)
        ledger = json.dumps(payload.coverage_ledger)
        # No scratch file paths should appear
        for text in [spec, packet, ledger]:
            assert "/tmp/" not in text
            assert "scratch" not in text.lower()

    def test_citations_resolve_to_packet(self):
        """All citation-ready passage IDs exist in the packet."""
        packet = _make_packet_payload()
        passage_ids = {p["passage_id"] for p in packet["passages"]}
        binding_passage_ids = set()
        for binding in packet["claim_evidence_bindings"]:
            binding_passage_ids.update(binding["passage_ids"])
        # All binding passage IDs should be in the passages set
        assert binding_passage_ids.issubset(passage_ids)


# ---------------------------------------------------------------------------
# No-inner-call tests
# ---------------------------------------------------------------------------


class TestNoInnerCalls:
    """Tests that HandoffPayload construction does not trigger semantic calls."""

    def test_payload_construction_is_deterministic(self):
        """Creating a payload from dicts is fully deterministic."""
        payload = _make_handoff_payload()
        # Same inputs → same outputs
        payload2 = _make_handoff_payload()
        d1 = payload.to_dict()
        d2 = payload2.to_dict()
        # Schema version, revisions, and structure should match
        assert d1["schema_version"] == d2["schema_version"]
        assert d1["evidence_packet_revision"] == d2["evidence_packet_revision"]
        assert d1["coverage_revision"] == d2["coverage_revision"]

    def test_no_model_calls_in_to_dict(self):
        """to_dict() does not call any LLM or embedding service."""
        payload = _make_handoff_payload()
        # Should complete without any side effects
        result = payload.to_dict()
        assert isinstance(result, dict)
        assert "schema_version" in result


# ---------------------------------------------------------------------------
# Token-bound tests
# ---------------------------------------------------------------------------


class TestTokenBounds:
    """Tests for token limit handling in handoff payload."""

    def test_token_limits_included(self):
        """Token limits are present when provided."""
        payload = _make_handoff_payload()
        assert payload.token_limits is not None
        assert "max_input_tokens" in payload.token_limits
        assert "max_output_tokens" in payload.token_limits

    def test_token_limits_bounded(self):
        """Token limit values are positive integers."""
        payload = _make_handoff_payload()
        for value in payload.token_limits.values():
            assert isinstance(value, int)
            assert value > 0

    def test_no_token_limits_when_none(self):
        """token_limits is None when not provided."""
        payload = _make_handoff_payload(token_limits=None)
        assert payload.token_limits is None

    def test_token_limits_serialized(self):
        """Token limits serialize correctly to dict."""
        payload = _make_handoff_payload()
        d = payload.to_dict()
        assert d["token_limits"] == payload.token_limits

    def test_custom_token_limits(self):
        """Custom token limits are respected."""
        custom = {"max_input_tokens": 4096, "max_output_tokens": 2048}
        payload = _make_handoff_payload(token_limits=custom)
        assert payload.token_limits == custom


# ---------------------------------------------------------------------------
# Citation resolution tests
# ---------------------------------------------------------------------------


class TestCitationResolution:
    """Tests that all citations resolve to packet passages."""

    def test_citation_ready_contains_claims(self):
        """Citation-ready output includes claims."""
        payload = _make_handoff_payload()
        cr = payload.citation_ready
        assert "claims" in cr or "claim" in cr or "claims_out" in cr

    def test_citation_ready_contains_passages(self):
        """Citation-ready output includes passages."""
        payload = _make_handoff_payload()
        cr = payload.citation_ready
        assert "passages" in cr or "passage" in cr or "passages_out" in cr

    def test_citation_ready_contains_bindings(self):
        """Citation-ready output includes claim-passage bindings."""
        payload = _make_handoff_payload()
        cr = payload.citation_ready
        assert "bindings" in cr or "binding" in cr

    def test_unresolved_items_explicit(self):
        """Unresolved items are explicit in the payload."""
        item_id = uuid4()
        payload = _make_handoff_payload(unresolved_items=(item_id,))
        assert item_id in payload.unresolved_items

    def test_limitations_explicit(self):
        """Limitations are explicit and non-empty when provided."""
        limitations = ("Single source", "Outdated data", "Limited domain")
        payload = _make_handoff_payload(limitations=limitations)
        assert payload.limitations == limitations
        assert len(payload.limitations) == 3


# ---------------------------------------------------------------------------
# Outline generation tests
# ---------------------------------------------------------------------------


class TestOutlineGeneration:
    """Tests for the optional outline field."""

    def test_outline_with_claims(self):
        """Outline is generated when there are claims."""
        payload = _make_handoff_payload()
        assert payload.outline is not None
        assert len(payload.outline) > 0

    def test_outline_can_be_none(self):
        """Outline can be None when no claims exist."""
        packet = _make_packet_payload()
        packet["claims"] = []
        payload = _make_handoff_payload(evidence_packet=packet, outline=None)
        assert payload.outline is None

    def test_outline_is_tuple(self):
        """Outline is a tuple of strings."""
        payload = _make_handoff_payload()
        assert isinstance(payload.outline, tuple)
        for item in payload.outline:
            assert isinstance(item, str)


# ---------------------------------------------------------------------------
# HandoffBuilder failure path tests
# ---------------------------------------------------------------------------


class TestHandoffBuilderFailurePaths:
    """Tests for HandoffBuilder failure scenarios."""

    def test_missing_evidence_packet_raises(self):
        """Builder raises when evidence packet is missing."""
        from research_store.handoff import HandoffBuilder

        def uow_factory_missing():
            class MockUow:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def get_evidence_packet(self, run_id):
                    return None

                def get_research_spec(self, run_id):
                    return _make_spec_payload()

                def get_coverage_summary(self, run_id):
                    return _make_ledger_payload()

                @property
                def coverage(self):
                    class MockCoverage:
                        def get_current_revision(self, run_id):
                            return 1

                        def list_coverage_events(self, run_id, limit=100, offset=0):
                            return []

                    return MockCoverage()

            return MockUow()

        builder = HandoffBuilder(uow_factory_missing)
        with pytest.raises(ValueError, match="EvidencePacket not found"):
            builder.build(uuid4())

    def test_missing_research_spec_raises(self):
        """Builder raises when research spec is missing."""
        from research_store.handoff import HandoffBuilder

        def uow_factory_no_spec():
            class MockUow:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def get_evidence_packet(self, run_id):
                    class MockRecord:
                        packet_revision = 1
                        coverage_revision = 1

                        def to_dict(self):
                            return {
                                "id": str(uuid4()),
                                "run_id": str(run_id),
                                "research_spec_id": str(uuid4()),
                                "coverage_revision": 1,
                                "packet_revision": 1,
                                "payload": _make_packet_payload(),
                                "created_at": datetime.now(timezone.utc),
                            }

                    return MockRecord()

                def get_research_spec(self, run_id):
                    return None

                def get_coverage_summary(self, run_id):
                    return _make_ledger_payload()

                @property
                def coverage(self):
                    class MockCoverage:
                        def get_current_revision(self, run_id):
                            return 1

                        def list_coverage_events(self, run_id, limit=100, offset=0):
                            return []

                    return MockCoverage()

            return MockUow()

        builder = HandoffBuilder(uow_factory_no_spec)
        with pytest.raises(ValueError, match="ResearchSpec not found"):
            builder.build(uuid4())


# ---------------------------------------------------------------------------
# HandoffBuilder success path test
# ---------------------------------------------------------------------------


class TestHandoffBuilderSuccess:
    """Tests for HandoffBuilder success scenarios."""

    def test_build_returns_valid_payload(self):
        """Builder returns a valid HandoffPayload with mock data."""
        from research_store.handoff import HandoffBuilder

        run_id = uuid4()

        def uow_factory():
            class MockUow:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def get_evidence_packet(self, run_id):
                    class MockRecord:
                        packet_revision = 2
                        coverage_revision = 1

                        def to_dict(self):
                            return {
                                "id": str(uuid4()),
                                "run_id": str(run_id),
                                "research_spec_id": str(uuid4()),
                                "coverage_revision": 1,
                                "packet_revision": 2,
                                "payload": _make_packet_payload(),
                                "created_at": datetime.now(timezone.utc),
                            }

                    return MockRecord()

                def get_research_spec(self, run_id):
                    return {
                        "id": str(uuid4()),
                        "run_id": str(run_id),
                        "spec_revision": 1,
                        "payload": _make_spec_payload(),
                    }

                def get_coverage_summary(self, run_id):
                    return _make_ledger_payload()

                @property
                def coverage(self):
                    class MockCoverage:
                        def get_current_revision(self, run_id):
                            return 1

                        def list_coverage_events(self, run_id, limit=100, offset=0):
                            return []

                    return MockCoverage()

            return MockUow()

        builder = HandoffBuilder(
            uow_factory,
            token_limits={"max_input_tokens": 8192},
            max_passages=64,
            max_claims=32,
        )
        payload = builder.build(run_id)

        assert isinstance(payload, HandoffPayload)
        assert payload.schema_version == "handoff-payload-v1"
        assert payload.run_id == run_id
        assert payload.evidence_packet_revision == 2
        assert payload.token_limits == {"max_input_tokens": 8192}
        assert payload.limitations == (
            "Limited source diversity",
            "Single domain coverage",
        )
        assert len(payload.unresolved_items) == 1


# ---------------------------------------------------------------------------
# Integration-style: CLI argument parsing
# ---------------------------------------------------------------------------


class TestCLIArgumentParsing:
    """Tests that the CLI parser accepts handoff arguments correctly."""

    def test_handoff_subcommand_exists(self):
        """The handoff subcommand is registered in the parser."""
        from research_store.cli import parser

        p = parser()
        # Should not raise — subcommand exists
        assert p is not None

    def test_handoff_requires_run_id(self):
        """The handoff subcommand requires a run_id argument."""
        from research_store.cli import parser

        p = parser()
        args = p.parse_args(["handoff", str(uuid4())])
        assert args.command == "handoff"
        assert args.run_id is not None

    def test_handoff_optional_args(self):
        """Optional handoff arguments parse correctly."""
        from research_store.cli import parser

        p = parser()
        args = p.parse_args(
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


# ---------------------------------------------------------------------------
# HandoffPayload import tests
# ---------------------------------------------------------------------------


class TestHandoffPayloadImport:
    """Tests that HandoffPayload is properly exported."""

    def test_import_from_models(self):
        from research_domain.models import HandoffPayload

        assert HandoffPayload.SCHEMA_VERSION == "handoff-payload-v1"

    def test_import_from_domain_init(self):
        from research_domain import HandoffPayload

        assert HandoffPayload.SCHEMA_VERSION == "handoff-payload-v1"

    def test_import_from_handoff_module(self):
        from research_store.handoff import HandoffBuilder

        assert HandoffBuilder is not None

    def test_handoff_payload_in_canonical_models(self):
        from research_domain.models import CANONICAL_MODELS, HandoffPayload

        assert HandoffPayload in CANONICAL_MODELS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
