"""Tests for staged semantic audit persistence (issue #33).

Covers:
- Domain model validation (invalid statuses, stages, sequences).
- Service-layer validation (unknown assessment, staleness).
- Partial audit: stage failures do not erase successful stages.
- Staleness: target hash changes make prior audits stale.
- Invalid references: invented evidence references fail validation.
- CLI parsing for audit subcommands.
- Export round-trip.
- Migration schema verification.
- Integration tests with disposable PostgreSQL.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.cli import parser as research_store_parser
from research_store.domain import (
    AuditAssessment,
    AuditStageOutput,
    VALID_AUDIT_STATUSES,
    VALID_AUDIT_STAGES,
    VALID_AUDIT_STAGE_STATUSES,
    VALID_AUDIT_TARGET_TYPES,
)
from research_store.service import AuditService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audit_uow(
    assessments: dict | None = None,
    stages: dict | None = None,
    stale: list[dict] | None = None,
):
    """Build a minimal UoW mock for AuditService unit tests."""
    assessments = assessments if assessments is not None else {}
    stages = stages if stages is not None else {}
    stale = stale if stale is not None else []

    class MockUoW:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create_audit_assessment(self, **kw):
            aid = uuid4()
            assessments[str(aid)] = kw
            return aid

        def get_audit_assessment(self, assessment_id):
            return assessments.get(str(assessment_id))

        def list_audit_assessments(self, **kw):
            return list(assessments.values())

        def insert_audit_stage_output(self, **kw):
            sid = uuid4()
            stages[str(sid)] = kw
            return sid

        def list_audit_stage_outputs(self, assessment_id=None, **kw):
            if assessment_id is None:
                assessment_id = kw.get("assessment_id")
            aid_str = str(assessment_id)
            return [
                s for s in stages.values()
                if str(s.get("assessment_id")) == aid_str
            ]

        def validate_assessment_exists(self, assessment_id):
            return str(assessment_id) in assessments

        def run_exists(self, run_id):
            return True

        def invocation_exists(self, invocation_id):
            return True

        def detect_stale_assessments(self, **kw):
            return stale

        def export_audit_assessment(self, assessment_id):
            assessment = assessments.get(str(assessment_id))
            if assessment is None:
                return None
            export = dict(assessment)
            export["stages"] = self.list_audit_stage_outputs(assessment_id)
            return export

        def validate_evidence_references(self, references):
            valid_set = getattr(self, "valid_evidence_refs", None)
            if valid_set is None:
                invalid = []
                for ref in references:
                    try:
                        UUID(str(ref))
                    except (ValueError, AttributeError):
                        invalid.append(str(ref))
                return invalid
            return [str(ref) for ref in references if str(ref) not in valid_set]

    return MockUoW()


# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


def test_audit_assessment_from_mapping():
    now = "2025-01-01T00:00:00+00:00"
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "run_id": "22222222-2222-2222-2222-222222222222",
        "target_type": "run",
        "target_id": "33333333-3333-3333-3333-333333333333",
        "target_hash": "abc123",
        "evaluator_version": "catalog-v5.0",
        "prompt_template_version": "staged-research-audit-v1",
        "policy_version": "audit-policy-v1",
        "stage_set": ["rubric", "acquisition", "evidence", "synthesis"],
        "status": "completed",
        "provider": "local",
        "model": "local-model",
        "prompt_hash": "prompt123",
        "model_fingerprint": "fp123",
        "elapsed_ms": 5000,
        "audit_packet_manifest": {"key": "value"},
        "created_at": now,
    }
    assessment = AuditAssessment.from_mapping(row)
    assert assessment.target_type == "run"
    assert assessment.status == "completed"
    assert assessment.stage_set == ("rubric", "acquisition", "evidence", "synthesis")
    d = assessment.to_dict()
    assert d["target_type"] == "run"
    assert d["stage_set"] == ["rubric", "acquisition", "evidence", "synthesis"]


def test_audit_assessment_rejects_invalid_target_type():
    with pytest.raises(ValueError, match="target_type"):
        AuditAssessment(
            id=uuid4(),
            run_id=uuid4(),
            target_type="bogus",
            target_id=uuid4(),
            target_hash="abc",
            evaluator_version="v1",
            prompt_template_version="v1",
            policy_version="v1",
            stage_set=("rubric",),
            status="completed",
        )


def test_audit_assessment_rejects_invalid_status():
    with pytest.raises(ValueError, match="status"):
        AuditAssessment(
            id=uuid4(),
            run_id=uuid4(),
            target_type="run",
            target_id=uuid4(),
            target_hash="abc",
            evaluator_version="v1",
            prompt_template_version="v1",
            policy_version="v1",
            stage_set=("rubric",),
            status="bogus",
        )


def test_audit_stage_output_from_mapping():
    now = "2025-01-01T00:00:00+00:00"
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "assessment_id": "22222222-2222-2222-2222-222222222222",
        "stage": "rubric",
        "sequence_number": 1,
        "status": "completed",
        "output": {"rubric": "test"},
        "error": None,
        "error_details": None,
        "call_count": 1,
        "used_fallback": False,
        "created_at": now,
    }
    stage = AuditStageOutput.from_mapping(row)
    assert stage.stage == "rubric"
    assert stage.status == "completed"
    assert stage.sequence_number == 1
    assert stage.output == {"rubric": "test"}
    d = stage.to_dict()
    assert d["stage"] == "rubric"


def test_audit_stage_output_rejects_invalid_stage():
    with pytest.raises(ValueError, match="stage"):
        AuditStageOutput(
            id=uuid4(),
            assessment_id=uuid4(),
            stage="bogus",
            sequence_number=1,
            status="completed",
        )


def test_audit_stage_output_rejects_invalid_status():
    with pytest.raises(ValueError, match="status"):
        AuditStageOutput(
            id=uuid4(),
            assessment_id=uuid4(),
            stage="rubric",
            sequence_number=1,
            status="bogus",
        )


def test_audit_stage_output_rejects_zero_sequence():
    with pytest.raises(ValueError, match="sequence_number"):
        AuditStageOutput(
            id=uuid4(),
            assessment_id=uuid4(),
            stage="rubric",
            sequence_number=0,
            status="completed",
        )


# ---------------------------------------------------------------------------
# Service validation tests (no database required)
# ---------------------------------------------------------------------------


def test_create_assessment_returns_uuid():
    uow = _make_audit_uow()
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="partial",
    )
    assert isinstance(aid, UUID)


def test_add_stage_output_rejects_unknown_assessment():
    uow = _make_audit_uow()
    svc = AuditService(lambda: uow)
    with pytest.raises(ValueError, match="assessment not found"):
        svc.add_stage_output(
            assessment_id=uuid4(),
            stage="rubric",
            sequence_number=1,
            status="completed",
        )


def test_add_stage_output_succeeds_for_known_assessment():
    assessments = {}
    uow = _make_audit_uow(assessments=assessments)
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="partial",
    )
    sid = svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"score": 1.0},
    )
    assert isinstance(sid, UUID)


def test_add_stage_output_rejects_invalid_evidence_references():
    assessments = {}
    uow = _make_audit_uow(assessments=assessments)
    uow.valid_evidence_refs = {"11111111-1111-1111-1111-111111111111"}
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["evidence"],
        status="partial",
    )
    # Valid evidence ref succeeds
    svc.add_stage_output(
        assessment_id=aid,
        stage="evidence",
        sequence_number=1,
        status="completed",
        output={"evidence_refs": ["11111111-1111-1111-1111-111111111111"]},
    )
    # Invalid evidence ref raises ValueError
    with pytest.raises(ValueError, match="invalid evidence references"):
        svc.add_stage_output(
            assessment_id=aid,
            stage="evidence",
            sequence_number=2,
            status="completed",
            output={"evidence_refs": ["invented-ref-id"]},
        )


def test_audit_sanitizes_secrets():
    assessments = {}
    stages = {}
    uow = _make_audit_uow(assessments=assessments, stages=stages)
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        audit_packet_manifest={"api_key": "secret-12345", "header": "Bearer secret_token_xyz"},
    )
    saved_assessment = uow.get_audit_assessment(aid)
    assert saved_assessment["audit_packet_manifest"]["api_key"] == "[REDACTED]"
    assert "[REDACTED]" in saved_assessment["audit_packet_manifest"]["header"]

    sid = svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"api_key": "secret-999"},
    )
    saved_stage = list(stages.values())[0]
    assert saved_stage["output"]["api_key"] == "[REDACTED]"


def test_partial_audit_preserves_successful_stages():
    """Stage failures do not erase successful stages."""
    assessments = {}
    stages = {}
    uow = _make_audit_uow(assessments=assessments, stages=stages)
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "acquisition"],
        status="partial",
    )
    # Rubric succeeds
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"rubric": "ok"},
    )
    # Acquisition fails
    svc.add_stage_output(
        assessment_id=aid,
        stage="acquisition",
        sequence_number=2,
        status="failed",
        error="model timeout",
    )
    # Both stages should be present
    stage_outputs = svc.get_stage_outputs(aid)
    assert len(stage_outputs) == 2
    assert stage_outputs[0]["status"] == "completed"
    assert stage_outputs[1]["status"] == "failed"


def test_staleness_detection():
    """Target hash changes make prior audits stale."""
    stale_data = [
        {"id": str(uuid4()), "target_hash": "old_hash", "status": "completed", "created_at": "2025-01-01"},
    ]
    uow = _make_audit_uow(stale=stale_data)
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    result = svc.detect_stale_assessments(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        current_hash="new_hash",
    )
    assert len(result) == 1
    assert result[0]["target_hash"] == "old_hash"


def test_export_assessment_includes_stages():
    """Export returns assessment + stage outputs."""
    assessments = {}
    stages = {}
    uow = _make_audit_uow(assessments=assessments, stages=stages)
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="partial",
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"rubric": "test"},
    )
    export = svc.export_assessment(aid)
    assert export is not None
    assert "stages" in export
    assert len(export["stages"]) == 1


def test_export_assessment_returns_none_for_unknown_id():
    uow = _make_audit_uow()
    svc = AuditService(lambda: uow)
    result = svc.export_assessment(uuid4())
    assert result is None


def test_assess_run_convenience_method():
    uow = _make_audit_uow()
    svc = AuditService(lambda: uow)
    run_id = uuid4()
    result = svc.assess_run(
        run_id=run_id,
        external_run_id="fr_test123",
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="partial",
    )
    assert "external_run_id" in result
    assert result["external_run_id"] == "fr_test123"


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------


def test_audit_parser():
    args = research_store_parser().parse_args([
        "audit", "fr_test",
        "--target-hash", "abc123",
    ])
    assert args.command == "audit"
    assert args.external_id == "fr_test"
    assert args.target_hash == "abc123"


def test_audit_status_parser():
    args = research_store_parser().parse_args([
        "audit-status", "fr_test",
    ])
    assert args.command == "audit-status"
    assert args.external_id == "fr_test"


def test_audit_query_parser():
    args = research_store_parser().parse_args([
        "audit-query", "fr_test",
    ])
    assert args.command == "audit-query"
    assert args.external_id == "fr_test"


def test_audit_export_parser():
    args = research_store_parser().parse_args([
        "audit-export", "fa_test123",
    ])
    assert args.command == "audit-export"
    assert args.assessment_id == "fa_test123"


def test_audit_staleness_parser():
    args = research_store_parser().parse_args([
        "audit-staleness", "fr_test",
        "--target-hash", "new_hash",
    ])
    assert args.command == "audit-staleness"
    assert args.target_hash == "new_hash"


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _ensure_run_exists(config, run_id):
    from research_store.postgres import connect
    with connect(config.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version, llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (str(run_id), "test request", "{}", "1.0", "test", "running", "created", "agent_led", "test request"),
        )


@INTEGRATION_MARK
def test_audit_assessment_lifecycle(tmp_path, prepared_database_for_audit):
    """Full lifecycle: create assessment, add stages, query, export."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)
    target_hash = "sha256_test_hash"

    # Create assessment
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash=target_hash,
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "acquisition", "evidence", "synthesis"],
        status="partial",
        provider="local",
        model="local-model",
        elapsed_ms=5000,
    )
    assert isinstance(aid, UUID)

    # Add stage outputs
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"rubric": "test rubric"},
        call_count=1,
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="acquisition",
        sequence_number=2,
        status="completed",
        output={"acquisition": "test acquisition"},
        call_count=2,
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="evidence",
        sequence_number=3,
        status="failed",
        error="model timeout",
        call_count=3,
    )

    # Query assessments
    assessments = svc.list_assessments(run_id=run_id)
    assert len(assessments) == 1
    assert assessments[0]["target_hash"] == target_hash

    # Query stage outputs
    stages = svc.get_stage_outputs(aid)
    assert len(stages) == 3
    assert stages[0]["status"] == "completed"
    assert stages[2]["status"] == "failed"

    # Export
    export = svc.export_assessment(aid)
    assert export is not None
    assert export["id"] == str(aid)
    assert len(export["stages"]) == 3


@INTEGRATION_MARK
def test_stale_assessment_retained_on_new_assessment(tmp_path, prepared_database_for_audit):
    """New assessment with different target_hash does not overwrite stale one."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)

    # First assessment with hash1
    aid1 = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
    )

    # Second assessment with hash2 (different target hash)
    aid2 = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash2",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
    )

    # Both should be queryable
    assessments = svc.list_assessments(run_id=run_id)
    assert len(assessments) == 2
    hashes = {a["target_hash"] for a in assessments}
    assert hashes == {"hash1", "hash2"}

    # Detect stale: hash2 is current, hash1 is stale
    stale = svc.detect_stale_assessments(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        current_hash="hash2",
    )
    assert len(stale) == 1
    assert stale[0]["target_hash"] == "hash1"


@INTEGRATION_MARK
def test_audit_status_filter(tmp_path, prepared_database_for_audit):
    """Assessments can be filtered by status."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)

    svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
    )
    svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash2",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="partial",
    )

    completed = svc.list_assessments(run_id=run_id, status="completed")
    assert len(completed) == 1

    partial = svc.list_assessments(run_id=run_id, status="partial")
    assert len(partial) == 1


@INTEGRATION_MARK
def test_audit_stage_filter_by_status(tmp_path, prepared_database_for_audit):
    """Stage outputs can be filtered by status."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "acquisition"],
        status="partial",
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="acquisition",
        sequence_number=2,
        status="failed",
        error="timeout",
    )

    completed_stages = svc.get_stage_outputs(aid, status="completed")
    assert len(completed_stages) == 1

    failed_stages = svc.get_stage_outputs(aid, status="failed")
    assert len(failed_stages) == 1
    assert failed_stages[0]["error"] == "timeout"


@INTEGRATION_MARK
def test_audit_stage_filter_by_stage_name(tmp_path, prepared_database_for_audit):
    """Stage outputs can be filtered by stage name."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)
    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "acquisition"],
        status="partial",
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="acquisition",
        sequence_number=2,
        status="completed",
    )

    rubric_stages = svc.get_stage_outputs(aid, stage="rubric")
    assert len(rubric_stages) == 1
    assert rubric_stages[0]["stage"] == "rubric"


@INTEGRATION_MARK
def test_audit_export_round_trip(tmp_path, prepared_database_for_audit):
    """Export can be round-tripped through import-like re-creation."""
    from research_store.container import build_audit_service
    from research_store.config import StoreConfig
    from dataclasses import replace

    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_audit_service(config)
    run_id = uuid4()
    _ensure_run_exists(config, run_id)

    aid = svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="hash1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        provider="local",
        model="local-model",
        elapsed_ms=1000,
    )
    svc.add_stage_output(
        assessment_id=aid,
        stage="rubric",
        sequence_number=1,
        status="completed",
        output={"rubric": "test"},
        call_count=1,
    )

    export = svc.export_assessment(aid)
    assert export is not None
    assert export["target_hash"] == "hash1"
    assert export["provider"] == "local"
    assert len(export["stages"]) == 1
    assert export["stages"][0]["output"] == {"rubric": "test"}


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

if TEST_DSN:
    from research_store.postgres import (
        connect,
        migrate,
        require_disposable_database_reset,
    )

    @pytest.fixture(scope="session")
    def prepared_database_for_audit():
        """Prepare database with migration 0018 applied."""
        require_disposable_database_reset(
            TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
        )
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 18

    def test_migration_0018_creates_audit_tables():
        """Verify migration 0018 creates audit_assessments and audit_stage_outputs."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT tablename FROM pg_tables
                WHERE schemaname='public'
                AND tablename IN ('audit_assessments', 'audit_stage_outputs')
                ORDER BY tablename"""
            )
            tables = {row[0] for row in cur.fetchall()}
            assert "audit_assessments" in tables
            assert "audit_stage_outputs" in tables

    def test_migration_0018_enum_types_exist():
        """Verify audit enums exist."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT typname FROM pg_type
                WHERE typname IN (
                    'audit_status', 'audit_stage',
                    'audit_stage_status', 'audit_target_type'
                )
                ORDER BY typname"""
            )
            types = {row[0] for row in cur.fetchall()}
            assert "audit_status" in types
            assert "audit_stage" in types
            assert "audit_stage_status" in types
            assert "audit_target_type" in types

    def test_migration_0018_indexes_exist():
        """Verify audit indexes exist."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT indexname FROM pg_indexes
                WHERE schemaname='public'
                AND indexname LIKE 'idx_audit_%'
                ORDER BY indexname"""
            )
            indexes = {row[0] for row in cur.fetchall()}
            expected = {
                "idx_audit_assessments_run",
                "idx_audit_assessments_target",
                "idx_audit_assessments_target_hash",
                "idx_audit_assessments_status",
                "idx_audit_stage_outputs_assessment",
                "idx_audit_stage_outputs_stage",
                "idx_audit_stage_outputs_status",
            }
            assert expected.issubset(indexes)

    def test_migration_0018_constraints_exist():
        """Verify audit constraints exist."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT conname FROM pg_constraint
                WHERE conrelid IN (
                    'audit_assessments'::regclass,
                    'audit_stage_outputs'::regclass
                )
                ORDER BY conname"""
            )
            constraints = {row[0] for row in cur.fetchall()}
            assert "chk_audit_assessments_target_hash" in constraints
            assert "chk_audit_assessments_status" in constraints
            assert "chk_audit_assessments_target_type" in constraints
            assert "uk_audit_assessments_target" in constraints
            assert "chk_audit_stage_outputs_sequence_number" in constraints
            assert "uk_audit_stage_outputs_assessment_stage_seq" in constraints

    def test_migration_0018_foreign_keys():
        """Verify audit foreign keys."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT conname, conrelid::regclass, confrelid::regclass
                FROM pg_constraint
                WHERE contype = 'f'
                AND conrelid::regclass::text IN ('audit_assessments', 'audit_stage_outputs')
                ORDER BY conname"""
            )
            fks = cur.fetchall()
            referenced_tables = {str(row[1]): str(row[2]) for row in fks}
            assert referenced_tables.get("audit_assessments") == "research_runs"
            assert referenced_tables.get("audit_stage_outputs") == "audit_assessments"

    def test_migration_0018_no_updated_at_column():
        """Verify audit_assessments does NOT have updated_at."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                WHERE table_name='audit_assessments'
                AND column_name='updated_at'"""
            )
            assert cur.fetchone() is None

    def test_migration_0018_audit_stage_outputs_cascade():
        """Verify audit_stage_outputs has ON DELETE CASCADE from audit_assessments."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT delete_rule FROM information_schema.referential_constraints
                WHERE constraint_name IN ('fk_audit_stage_outputs_assessment', 'audit_stage_outputs_assessment_id_fkey')"""
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "CASCADE"

    @INTEGRATION_MARK
    def test_detect_stale_assessments_integration(tmp_path, prepared_database_for_audit):
        """detect_stale_assessments works against real PostgreSQL."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        # Create assessment with hash1
        svc.create_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="hash1",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
        )

        # Create assessment with hash2
        svc.create_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="hash2",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
        )

        # With hash2 as current, hash1 is stale
        stale = svc.detect_stale_assessments(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            current_hash="hash2",
        )
        assert len(stale) == 1
        assert stale[0]["target_hash"] == "hash1"

        # With hash1 as current, hash2 is stale
        stale = svc.detect_stale_assessments(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            current_hash="hash1",
        )
        assert len(stale) == 1
        assert stale[0]["target_hash"] == "hash2"

        # With a new hash, both are stale
        stale = svc.detect_stale_assessments(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            current_hash="hash3",
        )
        assert len(stale) == 2

    @INTEGRATION_MARK
    def test_duplicate_assessment_rejected_by_unique_constraint(tmp_path, prepared_database_for_audit):
        """uk_audit_assessments_target prevents duplicate assessments for same target+hash."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace
        import psycopg

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        # First assessment succeeds
        aid1 = svc.create_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
        )

        # Duplicate assessment with same (run_id, target_type, target_id, target_hash) fails
        with pytest.raises(psycopg.IntegrityError):
            svc.create_assessment(
                run_id=run_id,
                target_type="run",
                target_id=run_id,
                target_hash="same_hash",
                evaluator_version="catalog-v5.0",
                prompt_template_version="staged-research-audit-v1",
                policy_version="audit-policy-v1",
                stage_set=["rubric"],
                status="completed",
            )

        # Same target with different hash succeeds
        aid2 = svc.create_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="different_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
        )
        assert aid2 != aid1


# ---------------------------------------------------------------------------
# Unit tests: audit identity hash & idempotent scheduling (issue #34)
# ---------------------------------------------------------------------------


def test_compute_audit_identity_hash_deterministic():
    """Same inputs produce the same identity hash."""
    from research_store.service import compute_audit_identity_hash

    h1 = compute_audit_identity_hash(
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "synthesis", "acquisition", "evidence"],
        model_fingerprint="fp-42",
    )
    h2 = compute_audit_identity_hash(
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["synthesis", "rubric", "evidence", "acquisition"],  # different order
        model_fingerprint="fp-42",
    )
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_compute_audit_identity_hash_different_inputs():
    """Different inputs produce different identity hashes."""
    from research_store.service import compute_audit_identity_hash

    h1 = compute_audit_identity_hash(
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        model_fingerprint="fp-42",
    )
    h2 = compute_audit_identity_hash(
        target_hash="abc123",
        evaluator_version="catalog-v5.1",  # different version
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        model_fingerprint="fp-42",
    )
    assert h1 != h2

    h3 = compute_audit_identity_hash(
        target_hash="different",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        model_fingerprint="fp-42",
    )
    assert h1 != h3

    h4 = compute_audit_identity_hash(
        target_hash="abc123",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        model_fingerprint=None,  # no fingerprint
    )
    assert h1 != h4


def test_schedule_assessment_reuse_existing():
    """Equivalent assessment → reuse, not create."""
    assessments = {}
    stages = {}

    class MockUoW:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create_audit_assessment(self, **kw):
            aid = uuid4()
            assessments[str(aid)] = kw
            return aid
        def get_audit_assessment(self, assessment_id):
            return assessments.get(str(assessment_id))
        def list_audit_assessments(self, **kw):
            return list(assessments.values())
        def insert_audit_stage_output(self, **kw):
            sid = uuid4()
            stages[str(sid)] = kw
            return sid
        def list_audit_stage_outputs(self, assessment_id=None, **kw):
            if assessment_id is None:
                assessment_id = kw.get("assessment_id")
            aid_str = str(assessment_id)
            return [s for s in stages.values() if str(s.get("assessment_id")) == aid_str]
        def validate_assessment_exists(self, assessment_id):
            return str(assessment_id) in assessments
        def run_exists(self, run_id):
            return True
        def invocation_exists(self, invocation_id):
            return True
        def detect_stale_assessments(self, **kw):
            return []
        def export_audit_assessment(self, assessment_id):
            assessment = assessments.get(str(assessment_id))
            if not assessment:
                return None
            return dict(assessment)
        def lookup_equivalent_assessment(self, audit_identity_hash):
            # Simulate: first call returns None, second call returns existing
            if not hasattr(self, "_lookup_called"):
                self._lookup_called = True
                return None
            return {
                "id": str(uuid4()),
                "run_id": "run-123",
                "target_type": "run",
                "target_id": "run-123",
                "target_hash": "abc",
                "evaluator_version": "catalog-v5.0",
                "prompt_template_version": "staged-research-audit-v1",
                "policy_version": "audit-policy-v1",
                "stage_set": ["rubric"],
                "status": "completed",
                "provider": "local",
                "model": None,
                "prompt_hash": None,
                "model_fingerprint": "fp-42",
                "elapsed_ms": 100,
                "audit_packet_manifest": None,
                "created_at": "2025-01-01T00:00:00Z",
                "audit_identity_hash": audit_identity_hash,
            }

    uow = MockUoW()
    svc = AuditService(lambda: uow)
    run_id = uuid4()

    # First call: no equivalent → create
    result1 = svc.schedule_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        model_fingerprint="fp-42",
    )
    assert result1["action"] == "create"
    assert result1["existing"] is False

    # Second call: equivalent exists → reuse
    result2 = svc.schedule_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        model_fingerprint="fp-42",
    )
    assert result2["action"] == "reuse"
    assert result2["existing"] is True
    # The assessment_id should be a valid UUID from the mock
    assert len(result2["assessment_id"]) == 36  # UUID hex format


def test_schedule_assessment_dry_run_no_match():
    """dry_run with no equivalent → dry_run_no_match."""
    assessments = {}
    stages = {}

    class MockUoW:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create_audit_assessment(self, **kw):
            aid = uuid4()
            assessments[str(aid)] = kw
            return aid
        def get_audit_assessment(self, assessment_id):
            return assessments.get(str(assessment_id))
        def list_audit_assessments(self, **kw):
            return list(assessments.values())
        def insert_audit_stage_output(self, **kw):
            sid = uuid4()
            stages[str(sid)] = kw
            return sid
        def list_audit_stage_outputs(self, assessment_id=None, **kw):
            if assessment_id is None:
                assessment_id = kw.get("assessment_id")
            aid_str = str(assessment_id)
            return [s for s in stages.values() if str(s.get("assessment_id")) == aid_str]
        def validate_assessment_exists(self, assessment_id):
            return str(assessment_id) in assessments
        def run_exists(self, run_id):
            return True
        def invocation_exists(self, invocation_id):
            return True
        def detect_stale_assessments(self, **kw):
            return []
        def export_audit_assessment(self, assessment_id):
            assessment = assessments.get(str(assessment_id))
            if not assessment:
                return None
            return dict(assessment)
        def lookup_equivalent_assessment(self, audit_identity_hash):
            return None  # No equivalent

    uow = MockUoW()
    svc = AuditService(lambda: uow)
    run_id = uuid4()

    result = svc.schedule_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        model_fingerprint="fp-42",
        dry_run=True,
    )
    assert result["action"] == "dry_run_no_match"
    assert result["existing"] is False
    # Should NOT have created an assessment
    assert len(assessments) == 0


def test_schedule_assessment_dry_run_match():
    """dry_run with equivalent → dry_run_match."""
    assessments = {}

    class MockUoW:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create_audit_assessment(self, **kw):
            aid = uuid4()
            assessments[str(aid)] = kw
            return aid
        def get_audit_assessment(self, assessment_id):
            return assessments.get(str(assessment_id))
        def list_audit_assessments(self, **kw):
            return list(assessments.values())
        def insert_audit_stage_output(self, **kw):
            return uuid4()
        def list_audit_stage_outputs(self, assessment_id=None, **kw):
            return []
        def validate_assessment_exists(self, assessment_id):
            return True
        def run_exists(self, run_id):
            return True
        def invocation_exists(self, invocation_id):
            return True
        def detect_stale_assessments(self, **kw):
            return []
        def export_audit_assessment(self, assessment_id):
            return {"id": str(assessment_id), "status": "completed"}
        def lookup_equivalent_assessment(self, audit_identity_hash):
            return {
                "id": str(uuid4()),
                "run_id": "run-123",
                "target_type": "run",
                "target_id": "run-123",
                "target_hash": "abc",
                "evaluator_version": "catalog-v5.0",
                "prompt_template_version": "staged-research-audit-v1",
                "policy_version": "audit-policy-v1",
                "stage_set": ["rubric"],
                "status": "completed",
                "provider": "local",
                "model": None,
                "prompt_hash": None,
                "model_fingerprint": "fp-42",
                "elapsed_ms": 100,
                "audit_packet_manifest": None,
                "created_at": "2025-01-01T00:00:00Z",
                "audit_identity_hash": audit_identity_hash,
            }

    uow = MockUoW()
    svc = AuditService(lambda: uow)
    run_id = uuid4()

    result = svc.schedule_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash="abc",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric"],
        status="completed",
        model_fingerprint="fp-42",
        dry_run=True,
    )
    assert result["action"] == "dry_run_match"
    assert result["existing"] is True
    assert result["assessment_id"]  # has a valid UUID


def test_cli_audit_parser_dry_run():
    """CLI --dry-run flag is parsed correctly."""
    args = research_store_parser().parse_args([
        "audit", "fr_test",
        "--target-hash", "abc123",
        "--dry-run",
    ])
    assert args.command == "audit"
    assert args.dry_run is True
    assert args.external_id == "fr_test"
    assert args.target_hash == "abc123"


# ---------------------------------------------------------------------------
# Integration tests: idempotent audit scheduling (issue #34)
# ---------------------------------------------------------------------------

class TestIdempotentScheduling:
    """Integration tests for idempotent audit scheduling against real PostgreSQL."""

    def test_schedule_assessment_reuse_integration(self, tmp_path, prepared_database_for_audit):
        """schedule_assessment reuses equivalent assessment."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        identity_kwargs = dict(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric", "synthesis"],
            status="completed",
            model_fingerprint="fp-42",
        )

        # First call: create
        result1 = svc.schedule_assessment(**identity_kwargs)
        assert result1["action"] == "create"
        assert result1["existing"] is False
        assessment_id_1 = result1["assessment_id"]
        identity_hash_1 = result1["audit_identity_hash"]

        # Second call: reuse
        result2 = svc.schedule_assessment(**identity_kwargs)
        assert result2["action"] == "reuse"
        assert result2["existing"] is True
        assert result2["assessment_id"] == assessment_id_1
        assert result2["audit_identity_hash"] == identity_hash_1

        # Verify only one assessment row exists
        assessments = svc.list_assessments(run_id=run_id)
        assert len(assessments) == 1

    def test_schedule_assessment_evidence_change_invalidates(self, tmp_path, prepared_database_for_audit):
        """Different target_hash → no reuse (evidence changed)."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        # First assessment with hash1
        result1 = svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="hash1",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )
        assert result1["action"] == "create"

        # Different target_hash → new assessment (not reuse)
        result2 = svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="hash2",  # different evidence
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )
        assert result2["action"] == "create"
        assert result2["existing"] is False

        # Both assessments should exist
        assessments = svc.list_assessments(run_id=run_id)
        assert len(assessments) == 2

    def test_schedule_assessment_model_fingerprint_change_invalidates(
        self, tmp_path, prepared_database_for_audit
    ):
        """Different model_fingerprint → no reuse."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )

        # Different model_fingerprint → new assessment
        result = svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-99",  # different
        )
        assert result["action"] == "create"
        assert result["existing"] is False

    def test_schedule_assessment_failed_not_reused(self, tmp_path, prepared_database_for_audit):
        """Equivalent but failed assessment → create new (not reuse)."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        identity_kwargs = dict(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            model_fingerprint="fp-42",
        )

        # Create failed assessment
        svc.create_assessment(
            **identity_kwargs,
            status="failed",
        )

        # Equivalent with status=completed → should create (failed excluded from partial unique)
        result = svc.schedule_assessment(
            **identity_kwargs,
            status="completed",
        )
        assert result["action"] == "create"
        assert result["existing"] is False

        # Both should exist
        assessments = svc.list_assessments(run_id=run_id)
        assert len(assessments) == 2

    def test_schedule_assessment_stage_set_change_invalidates(
        self, tmp_path, prepared_database_for_audit
    ):
        """Different stage_set → no reuse."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric", "synthesis"],
            status="completed",
            model_fingerprint="fp-42",
        )

        # Different stage_set → new assessment
        result = svc.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="same",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],  # different
            status="completed",
            model_fingerprint="fp-42",
        )
        assert result["action"] == "create"
        assert result["existing"] is False

    def test_schedule_assessment_concurrent_constraint(self, tmp_path, prepared_database_for_audit):
        """Concurrent schedule_assessment calls create only one assessment."""
        import threading
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        identity_kwargs = dict(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="concurrent_test",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )

        results = []
        errors = []

        def schedule():
            try:
                result = svc.schedule_assessment(**identity_kwargs)
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        # Launch 3 threads simultaneously
        threads = [threading.Thread(target=schedule) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No errors should have occurred
        assert not errors, f"Errors: {errors}"
        assert len(results) == 3

        # Count actions: should have exactly one "create" and two "reuse"
        actions = [r["action"] for r in results]
        assert actions.count("create") == 1, f"Expected 1 create, got: {actions}"
        assert actions.count("reuse") == 2, f"Expected 2 reuse, got: {actions}"

        # All results should share the same assessment_id
        assessment_ids = {r["assessment_id"] for r in results}
        assert len(assessment_ids) == 1, f"Expected 1 assessment_id, got: {assessment_ids}"

        # Verify only one row exists in the database
        assessments = svc.list_assessments(run_id=run_id)
        assert len(assessments) == 1

    def test_migration_0019_creates_identity_column(self, tmp_path, prepared_database_for_audit):
        """Migration 0019 adds audit_identity_hash column."""
        from research_store.config import StoreConfig
        from research_store.postgres import PostgresUnitOfWork
        from dataclasses import replace
        import psycopg

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        conn = psycopg.connect(TEST_DSN)
        try:
            conn.execute("SET search_path TO public")
            uow = PostgresUnitOfWork(
                config.database_url,
                config.physical_collection,
                config.embedding_model,
                config.embedding_revision,
                config.embedding_dimension,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            )
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'audit_assessments'
                    AND column_name = 'audit_identity_hash'"""
                )
                assert cur.fetchone() is not None, "audit_identity_hash column missing"
        finally:
            conn.close()

    def test_migration_0019_partial_unique_constraint(self, tmp_path, prepared_database_for_audit):
        """Migration 0019 adds partial unique constraint on audit_identity_hash."""
        from research_store.config import StoreConfig
        from research_store.postgres import PostgresUnitOfWork
        from dataclasses import replace
        import psycopg

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        conn = psycopg.connect(TEST_DSN)
        try:
            uow = PostgresUnitOfWork(
                config.database_url,
                config.physical_collection,
                config.embedding_model,
                config.embedding_revision,
                config.embedding_dimension,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            )
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT conname FROM pg_constraint
                    WHERE conrelid = 'audit_assessments'::regclass
                    AND conname = 'uk_audit_assessments_identity'"""
                )
                assert cur.fetchone() is not None, "partial unique constraint missing"
        finally:
            conn.close()

    def test_migration_0019_lookup_index(self, tmp_path, prepared_database_for_audit):
        """Migration 0019 adds lookup index on audit_identity_hash."""
        from research_store.config import StoreConfig
        from research_store.postgres import PostgresUnitOfWork
        from dataclasses import replace
        import psycopg

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        conn = psycopg.connect(TEST_DSN)
        try:
            uow = PostgresUnitOfWork(
                config.database_url,
                config.physical_collection,
                config.embedding_model,
                config.embedding_revision,
                config.embedding_dimension,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            )
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT indexname FROM pg_indexes
                    WHERE tablename = 'audit_assessments'
                    AND indexname = 'idx_audit_assessments_identity_hash'"""
                )
                assert cur.fetchone() is not None, "lookup index missing"
        finally:
            conn.close()

    def test_migration_0019_backfill_existing(
        self, tmp_path, prepared_database_for_audit
    ):
        """Migration 0019 backfills audit_identity_hash for existing rows."""
        from research_store.config import StoreConfig
        from research_store.postgres import PostgresUnitOfWork
        from research_store.service import AuditService, compute_audit_identity_hash
        from dataclasses import replace

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = AuditService(
            lambda: PostgresUnitOfWork(
                config.database_url,
                config.physical_collection,
                config.embedding_model,
                config.embedding_revision,
                config.embedding_dimension,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            )
        )
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        identity_kwargs = dict(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash="test_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
        )

        # Compute identity hash (same for both calls)
        identity_hash = compute_audit_identity_hash(
            target_hash="test_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
        )

        # First assessment (simulates pre-migration row, backfilled by migration)
        aid1 = svc.create_assessment(
            **identity_kwargs,
            audit_identity_hash=identity_hash,
        )

        # Second assessment with same identity → same hash
        aid2 = svc.create_assessment(
            **identity_kwargs,
            audit_identity_hash=identity_hash,
        )

        # Verify both assessments have audit_identity_hash populated
        assessment1 = svc.get_assessment(aid1)
        assert assessment1 is not None
        assert assessment1.get("audit_identity_hash") is not None
        assert len(assessment1["audit_identity_hash"]) == 64  # SHA-256 hex

        assessment2 = svc.get_assessment(aid2)
        assert assessment2 is not None
        assert assessment2.get("audit_identity_hash") == identity_hash

    def test_schedule_assessment_invocation_target_reuse(
        self, tmp_path, prepared_database_for_audit
    ):
        """Invocation targets (fc_*) are idempotent like run targets."""
        from research_store.container import build_audit_service
        from research_store.config import StoreConfig
        from dataclasses import replace
        from uuid import uuid4

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        # Create an invocation so fc_* resolution works
        from research_store.postgres import PostgresUnitOfWork

        uow = PostgresUnitOfWork(
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
        with uow.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_invocations
                    (id, run_id, external_invocation_id, operation, status, created_at)
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING id""",
                (
                    str(uuid4()),
                    str(run_id),
                    "fc_test_invocation",
                    "search",
                    "complete",
                ),
            )
            uow.connection.commit()

        # Schedule assessment for invocation target
        result1 = svc.schedule_assessment(
            run_id=run_id,
            target_type="invocation",
            target_id=uuid4(),  # will be created fresh below
            target_hash="invocation_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )
        assert result1["action"] == "create"

        # Equivalent invocation target → reuse
        result2 = svc.schedule_assessment(
            run_id=run_id,
            target_type="invocation",
            target_id=result1["assessment"].get(
                "id", result1.get("assessment_id")
            ),
            target_hash="invocation_hash",
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=["rubric"],
            status="completed",
            model_fingerprint="fp-42",
        )
        assert result2["action"] == "reuse"
        assert result2["existing"] is True

    def test_postgres_failure_does_not_rollback_filesystem(self, tmp_path):
        """catalog_v5.py audit_target persists filesystem assessment even
        when PostgreSQL persistence fails (FR-018 isolation).

        This test creates a minimal catalog target, then mocks
        research_store.config.StoreConfig.from_env to raise ImportError
        so both the pre-check and post-write PostgreSQL paths fail,
        falling through to the filesystem-based audit.
        """
        import json as json_mod
        from unittest.mock import patch

        # Set up a temporary catalog directory
        catalog_dir = tmp_path / "catalog"
        os.environ["FIRECRAWL_CATALOG_DIR"] = str(catalog_dir)

        # Create a minimal run target so build_audit_packet succeeds
        runs_dir = catalog_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        identifier = "fr_test_pg_failure"
        run_file = runs_dir / f"{identifier}.json"
        run_file.write_text(
            json_mod.dumps(
                {
                    "schema_version": 5,
                    "research_run_id": identifier,
                    "objective": "test objective",
                    "profile": {},
                    "lifecycle": {"state": "running", "revision": 1},
                    "record_revision": 1,
                    "invocation_ids": [],
                    "claims": [],
                    "used_sources": [],
                    "assessment_refs": [],
                    "audit_status": "stale",
                    "operational_status": "planning",
                    "data_completeness": 0.0,
                    "operational_summary": {},
                }
            )
        )

        from catalog_v5 import audit_target

        # Mock StoreConfig.from_env to raise ImportError (simulates
        # PostgreSQL unavailable) and mock the LLM calls to return
        # immediately with empty results.
        from types import SimpleNamespace

        mock_result = SimpleNamespace(
            value=None, provenance={}, attempts=[], error=""
        )

        with patch(
            "research_store.config.StoreConfig.from_env",
            side_effect=ImportError("no postgres"),
        ):
            with patch(
                "catalog_v5.call_structured",
                return_value=mock_result,
            ):
                audit_target(identifier, quiet=True)

        # Verify filesystem assessment was written despite PG failure
        assessment_dir = catalog_dir / "assessments" / identifier
        assessment_files = list(assessment_dir.glob("*.json"))
        assert len(assessment_files) > 0, (
            "filesystem assessment must be written when PostgreSQL fails"
        )
        assessment = json_mod.loads(assessment_files[0].read_text())
        assert assessment["target_id"] == identifier
        assert assessment["status"] in {"completed", "partial", "failed"}

        # Cleanup
        del os.environ["FIRECRAWL_CATALOG_DIR"]