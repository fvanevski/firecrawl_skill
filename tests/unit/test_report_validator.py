"""Tests for ReportValidator and ReportArtifactService (issue #64)."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from firecrawl_skill.research_store.reporting.artifacts import (
    ReportArtifactError,
    ReportArtifactService,
)
from firecrawl_skill.research_store.reporting.validation import (
    ReportValidationFinding,
    ReportValidationSeverity,
    ReportValidator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_PACKET = {
    "schema_version": "evidence-packet-v1",
    "run_id": "00000000-0000-0000-0000-000000000401",
    "research_spec_id": "00000000-0000-0000-0000-000000000100",
    "coverage_revision": 2,
    "claims": [
        {
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "statement": "The documented behavior is reproducible.",
            "semantic_status": "supported",
            "uncertainty": None,
        },
        {
            "claim_id": "00000000-0000-0000-0000-000000000103",
            "statement": "The feature is not production-ready.",
            "semantic_status": "unsupported",
            "uncertainty": "No production deployment data.",
        },
    ],
    "passages": [
        {
            "passage_id": "00000000-0000-0000-0000-000000000601",
            "candidate_id": "00000000-0000-0000-0000-000000000301",
            "snapshot_id": "00000000-0000-0000-0000-000000000602",
            "chunk_id": "00000000-0000-0000-0000-000000000603",
            "text": "The documented behavior is reproducible in test environments.",
            "source_url": "https://fixture.invalid/docs",
        },
        {
            "passage_id": "00000000-0000-0000-0000-000000000604",
            "candidate_id": "00000000-0000-0000-0000-000000000302",
            "snapshot_id": "00000000-0000-0000-0000-000000000605",
            "chunk_id": "00000000-0000-0000-0000-000000000606",
            "text": "Production deployment data is not yet available.",
            "source_url": "https://fixture.invalid/changelog",
        },
    ],
    "omitted_passages": [],
    "claim_evidence_bindings": [
        {
            "binding_id": "00000000-0000-0000-0000-000000000607",
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "passage_ids": ["00000000-0000-0000-0000-000000000601"],
            "relationship": "supports",
            "confidence": 0.9,
            "uncertainty": None,
        },
    ],
    "corroborating_groups": [],
    "contradicting_groups": [],
    "qualifying_groups": [],
    "near_duplicate_groups": [],
    "source_diversity_summary": {"independent_source_count": 2},
    "freshness_summary": {"status": "satisfied"},
    "limitations": [],
    "unresolved_items": [],
    "independence_assessments": [],
    "retrieval_provenance": [],
}


def _make_valid_report(
    packet_revision: int = 2,
    with_invented: bool = False,
    with_unsupported: bool = True,
    with_entailment_mismatch: bool = False,
) -> dict:
    """Build a valid report artifact for testing."""
    report: dict[str, Any] = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": packet_revision,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "passage_ids": [_VALID_PACKET["passages"][0]["passage_id"]],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    if with_invented:
        report["invented_citations"].append(
            {
                "section_id": "s2",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "passage_ids": ["00000000-0000-0000-0000-000000009999"],
            }
        )
        report["validation_results"].append(
            {
                "section_id": "s2",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "passage_ids": ["00000000-0000-0000-0000-000000009999"],
                "status": "invented",
                "issue": "Passage not in EvidencePacket",
            }
        )

    if with_unsupported:
        report["unsupported_claims"].append(
            {
                "claim_id": _VALID_PACKET["claims"][1]["claim_id"],
                "statement": _VALID_PACKET["claims"][1]["statement"],
            }
        )

    if with_entailment_mismatch:
        report["entailment_mismatches"].append(
            {
                "section_id": "s1",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "expected_relationship": "supports",
                "cited_relationship": "contradicts",
            }
        )

    return report


# ---------------------------------------------------------------------------
# ReportValidator — core tests
# ---------------------------------------------------------------------------


def test_validate_result_is_valid():
    """A valid report should produce an is_valid result."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is True
    assert result.is_complete is True
    assert result.stale_packet is False
    assert result.packet_revision == 2
    assert result.current_packet_revision == 2
    assert result.report_hash != ""
    assert len(result.claim_manifest) > 0


def test_validate_result_to_json():
    """to_json should produce valid JSON."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()
    data = json.loads(result.to_json())

    assert data["is_valid"] is True
    assert "report_hash" in data
    assert "claim_manifest" in data
    assert "errors" in data
    assert "warnings" in data


def test_validate_result_to_dict():
    """to_dict should produce a serializable dict."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()
    data = result.to_dict()

    assert isinstance(data, dict)
    assert data["is_valid"] is True
    assert data["report_hash"] == result.report_hash


def test_validate_result_summary():
    """Summary should be a human-readable one-liner."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert "valid" in result.summary.lower()
    assert result.report_hash[:8] in result.summary


# ---------------------------------------------------------------------------
# Stale-packet tests
# ---------------------------------------------------------------------------


def test_stale_packet_revision_is_error():
    """Report against an older packet revision should fail."""
    report = _make_valid_report(packet_revision=1)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert result.stale_packet is True
    assert any(f.code == "STALE_PACKET" for f in result.errors)
    assert "stale" in result.summary.lower()


def test_matching_packet_revision_is_ok():
    """Report against the current packet revision should pass."""
    report = _make_valid_report(packet_revision=2)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is True
    assert result.stale_packet is False
    assert not any(f.code == "STALE_PACKET" for f in result.errors)


# ---------------------------------------------------------------------------
# Invented-citation tests
# ---------------------------------------------------------------------------


def test_invented_citation_is_error():
    """Citations to unknown passage IDs should fail."""
    report = _make_valid_report(with_invented=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert any(f.code == "UNKNOWN_CITATION" for f in result.errors)
    assert len(result.errors) >= 1


def test_no_invented_citations_passes():
    """A report with no invented citations should not error."""
    report = _make_valid_report(with_invented=False)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert not any(f.code == "UNKNOWN_CITATION" for f in result.errors)


def test_invented_citation_in_validation_results():
    """Invented citations flagged in validation_results are also in the
    top-level invented_citations array which is the authoritative source.
    """
    report = _make_valid_report(with_invented=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert any(
        f.code == "UNKNOWN_CITATION"
        for f in result.errors
        if f.path and "invented_citations" in f.path
    )


def test_unknown_claim_citation_is_error():
    """Citations to unknown claim IDs should fail."""
    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000009999",
                "passage_ids": [],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert any(f.code == "UNKNOWN_CLAIM_CITATION" for f in result.errors)


# ---------------------------------------------------------------------------
# Unsupported-claim tests
# ---------------------------------------------------------------------------


def test_unsupported_claim_labeled():
    """Unsupported claims should be flagged in the claim manifest."""
    report = _make_valid_report(with_unsupported=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    unsupported = [cm for cm in result.claim_manifest if cm.resolution == "unsupported"]
    assert len(unsupported) >= 1

    unsupported_ids = [uc["claim_id"] for uc in report.get("unsupported_claims", [])]
    for cm in unsupported:
        if cm.resolution == "unsupported":
            assert cm.claim_id in unsupported_ids


def test_unsupported_claim_not_labeled_is_warning():
    """Unsupported claims in the report but not in unsupported_claims array should warn."""
    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": _VALID_PACKET["claims"][1]["claim_id"],
                "passage_ids": [],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert any(f.code == "UNSUPPORTED_CLAIM_NOT_LABELED" for f in result.warnings)


def test_no_unsupported_claims():
    """A report with no unsupported claims should pass."""
    report = _make_valid_report(with_unsupported=False)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert not any(f.code in ("UNSUPPORTED_CLAIM_NOT_LABELED",) for f in result.errors)


# ---------------------------------------------------------------------------
# Entailment-mismatch tests
# ---------------------------------------------------------------------------


def test_entailment_mismatch_is_error():
    """Entailment mismatches should fail validation."""
    report = _make_valid_report(with_entailment_mismatch=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert any(f.code == "ENTAILMENT_MISMATCH" for f in result.errors)


def test_no_entailment_mismatches():
    """A report with no entailment mismatches should pass."""
    report = _make_valid_report(with_entailment_mismatch=False)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert not any(f.code == "ENTAILMENT_MISMATCH" for f in result.errors)


def test_entailment_mismatch_in_validation_results():
    """Entailment mismatches flagged in validation_results should be caught."""
    report = _make_valid_report(with_entailment_mismatch=True)
    report["validation_results"].append(
        {
            "section_id": "s1",
            "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
            "passage_ids": [],
            "status": "entailment_mismatch",
            "issue": "Relationship mismatch",
        }
    )
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert any(
        f.code == "ENTAILMENT_MISMATCH"
        for f in result.errors
        if f.path and "validation_results" in f.path
    )


# ---------------------------------------------------------------------------
# Claim-coverage tests
# ---------------------------------------------------------------------------


def test_claim_manifest_includes_all_report_claims():
    """The claim manifest should include all claims referenced in the report."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    claim_ids = {cm.claim_id for cm in result.claim_manifest}
    assert _VALID_PACKET["claims"][0]["claim_id"] in claim_ids


def test_claim_not_in_packet_is_error():
    """A claim in the report but not in the packet should fail."""
    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000009999",
                "passage_ids": [],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert any(f.code == "UNKNOWN_REPORT_CLAIM" for f in result.errors)


def test_claim_no_binding_for_evaluated_claim():
    """An evaluated claim referenced in the report without a binding should error."""
    packet = deepcopy(_VALID_PACKET)
    new_claim_id = "00000000-0000-0000-0000-000000000104"
    packet["claims"].append(
        {
            "claim_id": new_claim_id,
            "statement": "A claim with no binding.",
            "semantic_status": "supported",
            "uncertainty": None,
        }
    )

    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": new_claim_id,
                "passage_ids": [],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    validator = ReportValidator(packet, report, current_packet_revision=2)
    result = validator.validate()

    assert any(f.code == "CLAIM_NO_BINDING" for f in result.errors)


# ---------------------------------------------------------------------------
# Report hash tests
# ---------------------------------------------------------------------------


def test_report_hash_is_deterministic():
    """The same report should produce the same hash."""
    report = _make_valid_report()
    validator1 = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    validator2 = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)

    result1 = validator1.validate()
    result2 = validator2.validate()

    assert result1.report_hash == result2.report_hash
    assert len(result1.report_hash) == 64


def test_report_hash_changes_with_content():
    """Different reports should produce different hashes."""
    report1 = _make_valid_report()
    report2 = _make_valid_report(with_invented=True)

    validator1 = ReportValidator(_VALID_PACKET, report1, current_packet_revision=2)
    validator2 = ReportValidator(_VALID_PACKET, report2, current_packet_revision=2)

    result1 = validator1.validate()
    result2 = validator2.validate()

    assert result1.report_hash != result2.report_hash


# ---------------------------------------------------------------------------
# ValidationFinding tests
# ---------------------------------------------------------------------------


def test_validation_finding_frozen():
    """ValidationFinding should be frozen (immutable)."""
    finding = ReportValidationFinding(
        code="TEST",
        severity=cast(ReportValidationSeverity, ReportValidationSeverity.ERROR),
        message="test message",
        path="test/path",
        detail={"key": "value"},
    )

    with pytest.raises(FrozenInstanceError):
        cast(Any, finding).code = "modified"


def test_validation_severity_enum():
    """ValidationSeverity should have the expected values."""
    assert ReportValidationSeverity.ERROR == "error"
    assert ReportValidationSeverity.WARNING == "warning"
    assert ReportValidationSeverity.INFO == "info"


# ---------------------------------------------------------------------------
# ReportValidationResult tests
# ---------------------------------------------------------------------------


def test_result_summary_for_stale_packet():
    """Summary should indicate stale packet."""
    report = _make_valid_report(packet_revision=1)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert "stale" in result.summary.lower()
    assert "rev 1" in result.summary
    assert "rev 2" in result.summary


def test_result_summary_for_valid():
    """Summary should indicate valid report."""
    report = _make_valid_report()
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert "valid" in result.summary.lower()
    assert "claims" in result.summary.lower()


def test_result_summary_for_invalid():
    """Summary should indicate invalid report with error count."""
    report = _make_valid_report(with_invented=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert "invalid" in result.summary.lower()
    assert "errors" in result.summary.lower()


# ---------------------------------------------------------------------------
# ReportArtifactService tests
# ---------------------------------------------------------------------------


def _make_mock_uow():
    """Build a mock UOW with explicit evidence-packet and synthesis roles."""
    mock_uow = MagicMock()
    mock_uow.runs.get_run_status.return_value = {
        "lifecycle_revision": 1,
        "execution_mode": "autonomous_local",
        "state": "synthesizing",
    }

    _records: dict[tuple[str, str], dict] = {}

    def _get_stage(run_id, stage_name):
        key = (str(run_id), stage_name)
        if key not in _records:
            raise KeyError(key)
        return _records[key]

    def _insert_stage(record):
        key = (str(record["run_id"]), record["stage_name"])
        _records[key] = record

    def _update_stage(record):
        key = (str(record["run_id"]), record["stage_name"])
        _records[key] = record

    def _get_stages(run_id=None):
        if run_id is None:
            return list(_records.values())
        return [v for k, v in _records.items() if k[0] == str(run_id)]

    _packet_store: dict[str, dict] = {}

    class _PacketRecord:
        __slots__ = ("packet_revision",)

        def __init__(self, packet_revision: int):
            self.packet_revision = packet_revision

    def _get_evidence_packet(run_id, packet_revision=None):
        key = str(run_id)
        if key not in _packet_store:
            return None
        pkt = _packet_store[key]
        return _PacketRecord(pkt["packet_revision"])

    def _persist_evidence_packet(run_id, *args, **kwargs):
        _packet_store[str(run_id)] = {
            "run_id": run_id,
            "packet_revision": args[3]
            if len(args) > 3
            else kwargs.get("packet_revision", 1),
        }

    mock_uow.evidence_packets.get_evidence_packet = _get_evidence_packet
    mock_uow.evidence_packets.persist_evidence_packet = _persist_evidence_packet
    mock_uow.synthesis_stages.get_synthesis_stage = _get_stage
    mock_uow.synthesis_stages.insert_synthesis_stage = _insert_stage
    mock_uow.synthesis_stages.update_synthesis_stage = _update_stage
    mock_uow.synthesis_stages.get_synthesis_stages = _get_stages

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_uow)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_uow_factory = MagicMock(return_value=mock_ctx)

    return mock_uow_factory, mock_uow, _records, _packet_store


def _make_evidence_service(mock_uow_factory, packet_store):
    """Build a mock EvidenceService."""
    mock_evidence = MagicMock()

    def export_packet(run_id, revision=None):
        pkt = packet_store.get(str(run_id))
        if pkt:
            return deepcopy(_VALID_PACKET)
        return None

    mock_evidence.export_packet = export_packet
    return mock_evidence


def test_validate_report_valid():
    """validate_report should return is_valid=True for a valid report."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    packet_store[str(_VALID_PACKET["run_id"])] = {
        "run_id": _VALID_PACKET["run_id"],
        "packet_revision": 2,
    }

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    report = _make_valid_report()
    result = service.validate_report(cast(UUID, _VALID_PACKET["run_id"]), report)

    assert result.is_valid is True
    assert result.stale_packet is False
    assert len(result.claim_manifest) > 0


def test_validate_report_stale_packet():
    """validate_report should detect stale packet revision."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    packet_store[str(_VALID_PACKET["run_id"])] = {
        "run_id": _VALID_PACKET["run_id"],
        "packet_revision": 2,
    }

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    report = _make_valid_report(packet_revision=1)
    result = service.validate_report(cast(UUID, _VALID_PACKET["run_id"]), report)

    assert result.is_valid is False
    assert result.stale_packet is True
    assert any(f.code == "STALE_PACKET" for f in result.errors)


def test_validate_report_packet_not_found():
    """validate_report should raise when EvidencePacket is not found."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    report = _make_valid_report()

    with pytest.raises(ReportArtifactError, match="EvidencePacket not found"):
        service.validate_report(cast(UUID, _VALID_PACKET["run_id"]), report)


def test_persist_validation_result():
    """persist_validation_result should insert a validation stage."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    packet_store[str(_VALID_PACKET["run_id"])] = {
        "run_id": _VALID_PACKET["run_id"],
        "packet_revision": 2,
    }

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    report = _make_valid_report()
    validation_result = service.validate_report(
        cast(UUID, _VALID_PACKET["run_id"]), report
    )
    record = service.persist_validation_result(
        cast(UUID, _VALID_PACKET["run_id"]), report, validation_result
    )

    assert record["stage_name"] == "validation"
    assert record["stage_status"] == "completed"
    assert record["artifact"]["report_hash"] == validation_result.report_hash
    assert record["artifact"]["validation_status"] == "valid"

    key = (str(_VALID_PACKET["run_id"]), "validation")
    assert key in _records


def test_get_report_returns_artifact():
    """get_report should return the validation artifact for a run."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    packet_store[str(_VALID_PACKET["run_id"])] = {
        "run_id": _VALID_PACKET["run_id"],
        "packet_revision": 2,
    }

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    report = _make_valid_report()
    validation_result = service.validate_report(
        cast(UUID, _VALID_PACKET["run_id"]), report
    )
    service.persist_validation_result(
        cast(UUID, _VALID_PACKET["run_id"]), report, validation_result
    )

    artifact = service.get_report(cast(UUID, _VALID_PACKET["run_id"]))
    assert artifact is not None
    assert artifact["report_hash"] == validation_result.report_hash
    assert artifact["validation_status"] == "valid"


def test_get_report_returns_none_when_not_found():
    """get_report should return None when no validation exists."""
    mock_uow_factory, _mock_uow, _records, packet_store = _make_mock_uow()
    evidence_service = _make_evidence_service(mock_uow_factory, packet_store)

    service = ReportArtifactService(mock_uow_factory, evidence_service)
    result = service.get_report(cast(UUID, _VALID_PACKET["run_id"]))
    assert result is None


# ---------------------------------------------------------------------------
# Combined failure-path tests
# ---------------------------------------------------------------------------


def test_stale_packet_with_invented_citations():
    """A stale report with invented citations should have multiple errors."""
    report = _make_valid_report(packet_revision=1, with_invented=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    error_codes = {f.code for f in result.errors}
    assert "STALE_PACKET" in error_codes
    assert "UNKNOWN_CITATION" in error_codes
    assert result.is_valid is False


def test_all_valid_no_findings():
    """A fully valid report should have no errors and minimal warnings."""
    report = _make_valid_report(
        with_invented=False,
        with_unsupported=True,
        with_entailment_mismatch=False,
    )
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is True
    assert result.is_complete is True
    assert len(result.errors) == 0


def test_validation_status_reflects_errors():
    """is_valid should be False when there are errors."""
    report = _make_valid_report(with_invented=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is False
    assert len(result.errors) > 0


def test_validation_status_reflects_warnings():
    """is_complete should be False when there are warnings."""
    report = _make_valid_report(with_unsupported=False)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    has_warnings = any(
        f.severity == ReportValidationSeverity.WARNING for f in result.warnings
    )
    if has_warnings:
        assert result.is_complete is False


def test_claim_manifest_resolution_mapping():
    """The claim manifest should correctly map claim resolution status."""
    report = _make_valid_report(with_unsupported=True)
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    supported = [
        cm
        for cm in result.claim_manifest
        if cm.claim_id == _VALID_PACKET["claims"][0]["claim_id"]
    ]
    assert len(supported) == 1
    assert supported[0].resolution == "supported"

    unsupported = [
        cm
        for cm in result.claim_manifest
        if cm.claim_id == _VALID_PACKET["claims"][1]["claim_id"]
    ]
    assert len(unsupported) == 1
    assert unsupported[0].resolution == "unsupported"


def test_citation_pass_schema_validation():
    """The validator should handle a report matching the citation-pass schema."""
    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "passage_ids": [_VALID_PACKET["passages"][0]["passage_id"]],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [
            {
                "claim_id": _VALID_PACKET["claims"][1]["claim_id"],
                "statement": _VALID_PACKET["claims"][1]["statement"],
            }
        ],
        "entailment_mismatches": [],
    }
    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert result.is_valid is True
    assert len(result.claim_manifest) == 2


def test_weak_passage_support_warning():
    """Claims with few shared terms between statement and passages should warn."""
    packet = deepcopy(_VALID_PACKET)
    packet["claims"].append(
        {
            "claim_id": "00000000-0000-0000-0000-000000000104",
            "statement": "The quantum entanglement protocol violates causality.",
            "semantic_status": "supported",
            "uncertainty": None,
        }
    )
    packet["passages"].append(
        {
            "passage_id": "00000000-0000-0000-0000-000000000607",
            "candidate_id": "00000000-0000-0000-0000-000000000303",
            "snapshot_id": "00000000-0000-0000-0000-000000000608",
            "chunk_id": "00000000-0000-0000-0000-000000000609",
            "text": "The documented behavior is reproducible in test environments.",
            "source_url": "https://fixture.invalid/docs2",
        }
    )

    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000104",
                "passage_ids": ["00000000-0000-0000-0000-000000000607"],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    validator = ReportValidator(packet, report, current_packet_revision=2)
    result = validator.validate()

    assert any(f.code == "WEAK_PASSAGE_SUPPORT" for f in result.warnings)


def test_strong_passage_support_no_warning():
    """Claims with good term overlap should not warn."""
    report = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(_VALID_PACKET["run_id"]),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "passage_ids": [_VALID_PACKET["passages"][0]["passage_id"]],
                "status": "valid",
                "issue": None,
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    validator = ReportValidator(_VALID_PACKET, report, current_packet_revision=2)
    result = validator.validate()

    assert not any(f.code == "WEAK_PASSAGE_SUPPORT" for f in result.warnings)


def test_extract_terms_handles_versions_and_hyphens():
    """_extract_terms should keep alphanumeric tokens ≥ 2 chars."""
    from firecrawl_skill.research_store.reporting.validation import _extract_terms

    assert "v2" in _extract_terms("v2.0")
    assert "7334" in _extract_terms("RFC-7334")
    assert "don" in _extract_terms("don't")
    assert "192" in _extract_terms("192.168.1.1")

    terms = _extract_terms("The documented behavior is reproducible")
    assert "documented" in terms
    assert "behavior" in terms
    assert "reproducible" in terms

    assert "v" not in _extract_terms("v2.0")
    assert "t" not in _extract_terms("don't")
