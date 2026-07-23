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

        def insert_audit_assessment_if_absent(self, **kw):
            if kw["status"] == "completed":
                for aid, existing in assessments.items():
                    if (
                        existing.get("run_id") == kw["run_id"]
                        and existing.get("target_type") == kw["target_type"]
                        and existing.get("target_id") == kw["target_id"]
                        and existing.get("audit_identity_hash")
                        == kw["audit_identity_hash"]
                        and existing.get("status") == "completed"
                    ):
                        return None
            return self.create_audit_assessment(**kw)

        def lookup_equivalent_assessment(
            self, run_id, target_type, target_id, audit_identity_hash
        ):
            for aid, existing in assessments.items():
                if (
                    existing.get("run_id") == run_id
                    and existing.get("target_type") == target_type
                    and existing.get("target_id") == target_id
                    and existing.get("audit_identity_hash") == audit_identity_hash
                    and existing.get("status") == "completed"
                ):
                    return {"id": aid, **existing}
            return None

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

        def validate_audit_target(self, run_id, target_type, target_id):
            return target_type == "invocation" or target_id == run_id

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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
        model_fingerprint="fp-test",
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
        "--model-fingerprint", "model-r1",
    ])
    assert args.command == "audit"
    assert args.external_id == "fr_test"
    assert args.target_hash == "abc123"
    assert args.model_fingerprint == "model-r1"


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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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
                model_fingerprint="fp-test",
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

    @pytest.fixture(scope="session", autouse=True)
    def prepared_database_for_audit():
        """Exercise both fresh-head and populated-0018 upgrade migrations."""
        require_disposable_database_reset(
            TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
        )

        # Fresh migration proof.
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        fresh_version = migrate(TEST_DSN)
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'audit_assessments'
                      AND column_name = 'audit_identity_hash'
                ), EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'audit_assessments'
                      AND indexname = 'uk_audit_assessments_completed_identity'
                )"""
            )
            fresh_column, fresh_index = cur.fetchone()

        # Populated 0018 upgrade proof. Leave this database at head for the
        # remainder of the integration suite.
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN, "0018_audit_assessments") == 18
        legacy_run_id = uuid4()
        legacy_assessment_id = uuid4()
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (
                    id, original_request, query_plan, skill_version, llm_model,
                    status, state, execution_mode, objective
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(legacy_run_id), "legacy", "{}", "1.0", "legacy",
                    "running", "created", "agent_led", "legacy",
                ),
            )
            cur.execute(
                """INSERT INTO audit_assessments (
                    id, run_id, target_type, target_id, target_hash,
                    evaluator_version, prompt_template_version, policy_version,
                    stage_set, status, provider, model
                ) VALUES (
                    %s, %s, 'run', %s, %s, %s, %s, %s, %s, 'completed', %s, %s
                )""",
                (
                    str(legacy_assessment_id),
                    str(legacy_run_id),
                    str(legacy_run_id),
                    "legacy-target-hash",
                    "catalog-v5.0",
                    "staged-research-audit-v1",
                    "audit-policy-v1",
                    ["rubric", "evidence"],
                    "local",
                    "legacy-model-r1",
                ),
            )
        upgraded_version = migrate(TEST_DSN)
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT target_hash, evaluator_version,
                          prompt_template_version, policy_version, stage_set,
                          model_fingerprint, audit_identity_hash
                   FROM audit_assessments WHERE id = %s""",
                (str(legacy_assessment_id),),
            )
            legacy_row = cur.fetchone()

        return {
            "fresh_version": fresh_version,
            "fresh_column": fresh_column,
            "fresh_index": fresh_index,
            "upgraded_version": upgraded_version,
            "legacy_run_id": legacy_run_id,
            "legacy_assessment_id": legacy_assessment_id,
            "legacy_row": legacy_row,
        }

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

    def test_migration_audit_constraints_at_head():
        """Verify retained constraints and the v19 replacement policy."""
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
            assert "uk_audit_assessments_target" not in constraints
            assert "chk_audit_assessments_model_fingerprint" in constraints
            assert "chk_audit_assessments_audit_identity_hash" in constraints
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
            model_fingerprint="fp-test",
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
            model_fingerprint="fp-test",
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
    def test_completed_create_is_idempotent_and_configuration_changes_are_distinct(
        tmp_path, prepared_database_for_audit
    ):
        from dataclasses import replace

        from research_store.config import StoreConfig
        from research_store.container import build_audit_service

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        svc = build_audit_service(config)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)
        common = {
            "run_id": run_id,
            "target_type": "run",
            "target_id": run_id,
            "target_hash": "same-hash",
            "evaluator_version": "catalog-v5.0",
            "prompt_template_version": "staged-research-audit-v1",
            "policy_version": "audit-policy-v1",
            "stage_set": ["rubric"],
            "status": "completed",
            "model_fingerprint": "fp-test",
        }

        first = svc.create_assessment(**common)
        duplicate = svc.create_assessment(**common)
        changed_model = svc.create_assessment(
            **{**common, "model_fingerprint": "fp-test-r2"}
        )
        changed_evaluator = svc.create_assessment(
            **{**common, "evaluator_version": "catalog-v5.1"}
        )

        assert duplicate == first
        assert changed_model != first
        assert changed_evaluator != first



# ---------------------------------------------------------------------------
# Audit identity and idempotent scheduling (issue #34)
# ---------------------------------------------------------------------------


def test_audit_identity_hash_is_canonical_and_stage_order_independent():
    from research_store.service import compute_audit_identity_hash

    common = {
        "target_hash": "target-hash",
        "evaluator_version": "catalog-v5.0",
        "prompt_template_version": "staged-research-audit-v1",
        "policy_version": "audit-policy-v1",
        "model_fingerprint": "model-fingerprint-r1",
    }
    first = compute_audit_identity_hash(
        **common, stage_set=["synthesis", "rubric", "evidence"]
    )
    second = compute_audit_identity_hash(
        **common, stage_set=["evidence", "synthesis", "rubric", "rubric"]
    )
    assert first == second
    assert len(first) == 64


def test_model_fingerprint_is_required_or_derived_from_fixed_model():
    from research_store.service import resolve_model_fingerprint

    explicit = resolve_model_fingerprint(
        model_fingerprint=" provider-issued-r1 ",
        provider=None,
        model=None,
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
    )
    assert explicit == "provider-issued-r1"

    derived = resolve_model_fingerprint(
        model_fingerprint=None,
        provider="local",
        model="qwen-r1",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
    )
    assert len(derived) == 64
    assert derived != resolve_model_fingerprint(
        model_fingerprint=None,
        provider="local",
        model="qwen-r2",
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
    )

    with pytest.raises(ValueError, match="model_fingerprint is required"):
        resolve_model_fingerprint(
            model_fingerprint=None,
            provider="local",
            model=None,
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
        )


@INTEGRATION_MARK
class TestIdempotentScheduling:
    def _service(self, tmp_path):
        from dataclasses import replace

        from research_store.config import StoreConfig
        from research_store.container import build_audit_service

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=tmp_path / "blobs",
        )
        return config, build_audit_service(config)

    @staticmethod
    def _identity(run_id, **overrides):
        values = {
            "run_id": run_id,
            "target_type": "run",
            "target_id": run_id,
            "target_hash": "target-hash",
            "evaluator_version": "catalog-v5.0",
            "prompt_template_version": "staged-research-audit-v1",
            "policy_version": "audit-policy-v1",
            "stage_set": ["rubric", "evidence", "synthesis"],
            "status": "completed",
            "provider": "local",
            "model": "qwen-r1",
            "model_fingerprint": "qwen-r1-fingerprint",
        }
        values.update(overrides)
        return values

    def test_fresh_and_populated_upgrade_paths(self, prepared_database_for_audit):
        from research_store.service import compute_audit_identity_hash

        evidence = prepared_database_for_audit
        assert evidence["fresh_version"] >= 19
        assert evidence["fresh_column"] is True
        assert evidence["fresh_index"] is True
        assert evidence["upgraded_version"] >= 19

        (
            target_hash,
            evaluator_version,
            prompt_template_version,
            policy_version,
            stage_set,
            model_fingerprint,
            audit_identity_hash,
        ) = evidence["legacy_row"]
        assert model_fingerprint
        assert audit_identity_hash == compute_audit_identity_hash(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=list(stage_set),
            model_fingerprint=model_fingerprint,
        )

    def test_completed_reuse_and_configuration_invalidation(
        self, tmp_path, prepared_database_for_audit
    ):
        config, svc = self._service(tmp_path)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        created = svc.schedule_assessment(**self._identity(run_id))
        reused = svc.schedule_assessment(**self._identity(run_id))
        assert created["action"] == "create"
        assert reused["action"] == "reuse"
        assert reused["assessment_id"] == created["assessment_id"]

        changed_target = svc.schedule_assessment(
            **self._identity(run_id, target_hash="changed-target")
        )
        changed_model = svc.schedule_assessment(
            **self._identity(
                run_id,
                model="qwen-r2",
                model_fingerprint="qwen-r2-fingerprint",
            )
        )
        changed_stages = svc.schedule_assessment(
            **self._identity(run_id, stage_set=["rubric"])
        )
        assert {changed_target["action"], changed_model["action"], changed_stages["action"]} == {"create"}

    def test_partial_attempt_is_not_reused_and_completed_retry_wins(
        self, tmp_path, prepared_database_for_audit
    ):
        config, svc = self._service(tmp_path)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)

        partial = svc.schedule_assessment(
            **self._identity(run_id, status="partial")
        )
        completed = svc.schedule_assessment(**self._identity(run_id))
        reused = svc.schedule_assessment(**self._identity(run_id))

        assert partial["action"] == "create"
        assert completed["action"] == "create"
        assert reused["action"] == "reuse"
        assert reused["assessment_id"] == completed["assessment_id"]
        assert reused["assessment_id"] != partial["assessment_id"]

    def test_unknown_and_mismatched_targets_are_rejected(
        self, tmp_path, prepared_database_for_audit
    ):
        config, svc = self._service(tmp_path)
        run_id = uuid4()
        other_run_id = uuid4()
        _ensure_run_exists(config, run_id)
        _ensure_run_exists(config, other_run_id)

        with pytest.raises(ValueError, match="not found or not owned"):
            svc.schedule_assessment(**self._identity(uuid4()))

        with pytest.raises(ValueError, match="not found or not owned"):
            svc.schedule_assessment(
                **self._identity(run_id, target_id=other_run_id)
            )

        with pytest.raises(ValueError, match="not found or not owned"):
            svc.schedule_assessment(
                **self._identity(
                    run_id,
                    target_type="invocation",
                    target_id=uuid4(),
                )
            )

    def test_concurrent_completed_scheduling_creates_one(
        self, tmp_path, prepared_database_for_audit
    ):
        import threading

        config, svc = self._service(tmp_path)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def schedule():
            try:
                barrier.wait()
                results.append(svc.schedule_assessment(**self._identity(run_id)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=schedule) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert [item["action"] for item in results].count("create") == 1
        assert [item["action"] for item in results].count("reuse") == 2
        assert len({item["assessment_id"] for item in results}) == 1

    def test_export_preserves_every_provenance_field(
        self, tmp_path, prepared_database_for_audit
    ):
        config, svc = self._service(tmp_path)
        run_id = uuid4()
        _ensure_run_exists(config, run_id)
        result = svc.schedule_assessment(
            **self._identity(run_id),
            prompt_hash="prompt-sha256",
            elapsed_ms=321,
            audit_packet_manifest={
                "schema_version": "audit-packet-v1",
                "source_ids": ["source-1"],
            },
        )
        exported = result["assessment"]

        assert exported["run_id"] == str(run_id)
        assert exported["target_type"] == "run"
        assert exported["target_id"] == str(run_id)
        assert exported["target_hash"] == "target-hash"
        assert exported["evaluator_version"] == "catalog-v5.0"
        assert exported["prompt_template_version"] == "staged-research-audit-v1"
        assert exported["policy_version"] == "audit-policy-v1"
        assert tuple(exported["stage_set"]) == ("rubric", "evidence", "synthesis")
        assert exported["status"] == "completed"
        assert exported["provider"] == "local"
        assert exported["model"] == "qwen-r1"
        assert exported["prompt_hash"] == "prompt-sha256"
        assert exported["model_fingerprint"] == "qwen-r1-fingerprint"
        assert exported["elapsed_ms"] == 321
        assert exported["audit_packet_manifest"]["schema_version"] == "audit-packet-v1"
        assert len(exported["audit_identity_hash"]) == 64
        assert exported["created_at"] is not None
