"""ReportArtifactService — persistence for report artifacts and claim manifests.

This module provides a service for persisting report artifacts, claim
manifests, and validation results to PostgreSQL (issue #64).

## Architecture

- PostgreSQL is the authoritative store for report artifacts.
- Report artifacts are persisted as ``validation`` synthesis stages,
  linking to the EvidencePacket revision and synthesis model call.
- The service is idempotent: ``validate_report()`` is a pure function of
  packet + report and always produces the same result.
- ``persist_validation_result()`` inserts a new row each time (new
  ``stage_id``).  The caller is responsible for deduplication — in practice
  the synthesis pipeline calls this once per run after a successful
  validation pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from .validation import (
    ReportValidationResult,
    ReportValidator,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReportArtifactError(RuntimeError):
    """Base exception for ReportArtifactService errors."""


class ReportArtifactService:
    """Persists report artifacts, claim manifests, and validation results.

    Args:
        uow_factory: Callable returning a UOW context.
        evidence_service: EvidenceService for EvidencePacket access.
    """

    def __init__(self, uow_factory, evidence_service) -> None:
        self._uow_factory = uow_factory
        self._evidence = evidence_service

    def validate_report(
        self,
        run_id: UUID,
        report: dict[str, Any],
    ) -> ReportValidationResult:
        """Validate a report artifact against its EvidencePacket."""
        packet_revision = report.get("evidence_packet_revision", 1)
        packet_record = self._evidence.export_packet(run_id, packet_revision)
        if packet_record is None:
            raise ReportArtifactError(
                f"EvidencePacket not found for run {run_id} revision {packet_revision}"
            )
        packet = packet_record.get("payload", packet_record)
        current_revision = self._get_current_packet_revision(run_id)
        validator = ReportValidator(packet, report, current_revision)
        return validator.validate()

    def persist_validation_result(
        self,
        run_id: UUID,
        report: dict[str, Any],
        validation_result: ReportValidationResult,
        *,
        model_name: str = "deterministic-report-validator-v1",
        prompt_version: str = "synthesis-v1",
    ) -> dict[str, Any]:
        """Persist a report artifact and its validation result as a synthesis stage."""
        now = _utcnow()
        stage_id = uuid4()
        semantic_call_id: UUID | None = None

        record: dict[str, Any] = {
            "id": stage_id,
            "run_id": run_id,
            "stage_name": "validation",
            "stage_status": "completed" if validation_result.is_valid else "failed",
            "semantic_call_id": semantic_call_id,
            "semantic_artifact_id": None,
            "evidence_packet_revision": validation_result.packet_revision,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "schema_version": 1,
            "artifact": {
                "report_hash": validation_result.report_hash,
                "current_packet_revision": validation_result.current_packet_revision,
                "stale_packet": validation_result.stale_packet,
                "validation_status": (
                    "valid" if validation_result.is_valid else "invalid"
                ),
                "is_complete": validation_result.is_complete,
                "claim_manifest": [
                    {
                        "claim_id": cm.claim_id,
                        "statement": cm.statement,
                        "resolution": cm.resolution,
                        "cited_passage_ids": list(cm.cited_passage_ids),
                        "binding_relationship": cm.binding_relationship,
                        "issues": list(cm.issues),
                    }
                    for cm in validation_result.claim_manifest
                ],
                "validation_errors_count": len(validation_result.errors),
                "validation_warnings_count": len(validation_result.warnings),
                "summary": validation_result.summary,
            },
            "error": None if validation_result.is_valid else validation_result.summary,
            "attempts": 1,
            "created_at": now,
            "updated_at": now,
        }

        with self._uow_factory() as uow:
            try:
                try:
                    existing = uow.synthesis_stages.get_synthesis_stage(
                        run_id, "validation"
                    )
                except KeyError:
                    existing = None
                if existing is None:
                    uow.synthesis_stages.insert_synthesis_stage(record)
                else:
                    record["id"] = existing["id"]
                    record["created_at"] = existing["created_at"]
                    record["attempts"] = int(existing.get("attempts", 0)) + 1
                    uow.synthesis_stages.update_synthesis_stage(record)
            except Exception as exc:
                raise ReportArtifactError(
                    f"failed to persist validation for run {run_id}: {exc}"
                ) from exc

        return record

    def get_report(
        self,
        run_id: UUID,
    ) -> dict[str, Any] | None:
        """Retrieve the validation result for a run."""
        with self._uow_factory() as uow:
            try:
                record = uow.synthesis_stages.get_synthesis_stage(
                    run_id, "validation"
                )
                if record:
                    return record.get("artifact")
                return None
            except KeyError:
                return None

    def _get_current_packet_revision(self, run_id: UUID) -> int:
        """Get the most recent EvidencePacket revision for a run."""
        with self._uow_factory() as uow:
            latest = uow.evidence_packets.get_evidence_packet(run_id)
            if latest:
                return latest.packet_revision
            return 1
