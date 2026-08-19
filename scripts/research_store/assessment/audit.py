"""Authoritative staged audit service for the assessment vertical slice."""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any
from uuid import UUID

from ..invocation_events import _sanitize

AUDIT_IDENTITY_VERSION = "audit-identity-v1"
AUDIT_MODEL_IMPLEMENTATION_VERSION = "audit-evaluator-v1"


def resolve_model_fingerprint(
    *,
    model_fingerprint: str | None,
    provider: str | None,
    model: str | None,
    evaluator_version: str,
    prompt_template_version: str,
) -> str:
    """Return a stable, non-empty evaluator/model fingerprint."""
    if model_fingerprint is not None:
        fingerprint = model_fingerprint.strip()
        if not fingerprint:
            raise ValueError("model_fingerprint must be non-empty")
        return fingerprint
    if not provider or not provider.strip() or not model or not model.strip():
        raise ValueError(
            "model_fingerprint is required unless provider and model are both supplied"
        )
    identity = {
        "implementation_version": AUDIT_MODEL_IMPLEMENTATION_VERSION,
        "provider": provider.strip(),
        "model": model.strip(),
        "evaluator_version": evaluator_version,
        "prompt_template_version": prompt_template_version,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def compute_audit_identity_hash(
    *,
    target_hash: str,
    evaluator_version: str,
    prompt_template_version: str,
    policy_version: str,
    stage_set: list[str],
    model_fingerprint: str,
) -> str:
    """Compute the canonical SHA-256 identity for reusable audit output."""
    required = {
        "target_hash": target_hash,
        "evaluator_version": evaluator_version,
        "prompt_template_version": prompt_template_version,
        "policy_version": policy_version,
        "model_fingerprint": model_fingerprint,
    }
    empty = [name for name, value in required.items() if not value or not value.strip()]
    if empty:
        raise ValueError("audit identity fields must be non-empty: " + ", ".join(empty))
    normalized_stages = sorted(set(stage_set))
    if not normalized_stages or any(
        not stage or not stage.strip() for stage in normalized_stages
    ):
        raise ValueError("stage_set must contain non-empty stages")
    identity = {
        "identity_version": AUDIT_IDENTITY_VERSION,
        "evaluator_version": evaluator_version,
        "model_fingerprint": model_fingerprint,
        "policy_version": policy_version,
        "prompt_template_version": prompt_template_version,
        "stage_set": normalized_stages,
        "target_hash": target_hash,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _extract_evidence_references(obj: Any) -> list[str]:
    """Recursively extract evidence IDs from a structured stage output."""
    refs: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()
            if key_lower in (
                "evidence_refs",
                "evidence_references",
                "claim_id",
                "claim_ids",
                "passage_id",
                "passage_ids",
                "snapshot_id",
                "snapshot_ids",
            ):
                if isinstance(value, (list, tuple, set)):
                    refs.extend(str(item) for item in value if item)
                elif value:
                    refs.append(str(value))
            else:
                refs.extend(_extract_evidence_references(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            refs.extend(_extract_evidence_references(item))
    return refs


class AuditService:
    """Authoritative service for staged semantic audit persistence.

    Persists audit assessments and their individual stage outputs in
    PostgreSQL. Stage failures do not erase successful stages. Target
    hash changes make prior assessments stale but they remain as
    historical records.
    """

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory

    def _identity(
        self,
        *,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        provider: str | None,
        model: str | None,
        model_fingerprint: str | None,
    ) -> tuple[str, str]:
        fingerprint = resolve_model_fingerprint(
            model_fingerprint=model_fingerprint,
            provider=provider,
            model=model,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
        )
        identity_hash = compute_audit_identity_hash(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            model_fingerprint=fingerprint,
        )
        return fingerprint, identity_hash

    @staticmethod
    def _validate_target(uow, run_id: UUID, target_type: str, target_id: UUID) -> None:
        if target_type not in {"run", "invocation"}:
            raise ValueError(f"invalid audit target_type: {target_type}")
        if not uow.validate_audit_target(run_id, target_type, target_id):
            raise ValueError(
                f"audit target not found or not owned by run: "
                f"{run_id}/{target_type}/{target_id}"
            )

    def create_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> UUID:
        """Append an assessment with an internally computed identity."""
        fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        sanitized_manifest = (
            _sanitize(audit_packet_manifest) if audit_packet_manifest else None
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            assessment_id = uow.insert_audit_assessment_if_absent(
                run_id=run_id,
                target_type=target_type,
                target_id=target_id,
                target_hash=target_hash,
                evaluator_version=evaluator_version,
                prompt_template_version=prompt_template_version,
                policy_version=policy_version,
                stage_set=stage_set,
                status=status,
                audit_identity_hash=identity_hash,
                provider=provider,
                model=model,
                prompt_hash=prompt_hash,
                model_fingerprint=fingerprint,
                elapsed_ms=elapsed_ms,
                audit_packet_manifest=sanitized_manifest,
            )
            if assessment_id is not None:
                return assessment_id
            existing = uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )
            if existing is None:
                raise RuntimeError(
                    "completed audit conflict did not resolve to an assessment"
                )
            return UUID(existing["id"])

    def add_stage_output(
        self,
        assessment_id: UUID,
        stage: str,
        sequence_number: int,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        error_details: dict[str, Any] | None = None,
        call_count: int = 0,
        used_fallback: bool = False,
    ) -> UUID:
        """Add one persistent audit stage output."""
        sanitized_output = _sanitize(output) if output else None
        sanitized_error_details = _sanitize(error_details) if error_details else None

        with self.uow_factory() as uow:
            if not uow.validate_assessment_exists(assessment_id):
                raise ValueError(f"assessment not found: {assessment_id}")

            if sanitized_output:
                extracted_refs = _extract_evidence_references(sanitized_output)
                if extracted_refs:
                    invalid_refs = uow.validate_evidence_references(extracted_refs)
                    if invalid_refs:
                        raise ValueError(
                            "invalid evidence references in stage output: "
                            f"{sorted(set(invalid_refs))}"
                        )

            stage_id = uow.insert_audit_stage_output(
                assessment_id=assessment_id,
                stage=stage,
                sequence_number=sequence_number,
                status=status,
                output=sanitized_output,
                error=error,
                error_details=sanitized_error_details,
                call_count=call_count,
                used_fallback=used_fallback,
            )
        return stage_id

    def get_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        """Fetch a single audit assessment by ID."""
        with self.uow_factory() as uow:
            return uow.get_audit_assessment(assessment_id)

    def list_assessments(
        self,
        run_id: UUID | None = None,
        target_id: UUID | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List audit assessments with optional filters."""
        with self.uow_factory() as uow:
            return uow.list_audit_assessments(
                run_id=run_id,
                target_id=target_id,
                status=status,
                limit=limit,
                offset=offset,
            )

    def get_stage_outputs(
        self,
        assessment_id: UUID,
        stage: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List stage outputs for an assessment."""
        with self.uow_factory() as uow:
            return uow.list_audit_stage_outputs(
                assessment_id=assessment_id,
                stage=stage,
                status=status,
                limit=limit,
                offset=offset,
            )

    def detect_stale_assessments(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        current_hash: str,
    ) -> list[dict[str, Any]]:
        """Return assessments whose target hash differs from current state."""
        if target_type == "run":
            with self.uow_factory() as uow:
                if not uow.run_exists(run_id):
                    raise ValueError(f"run not found: {run_id}")
        elif target_type == "invocation":
            with self.uow_factory() as uow:
                if not uow.invocation_exists(target_id):
                    raise ValueError(f"invocation not found: {target_id}")
        with self.uow_factory() as uow:
            return uow.detect_stale_assessments(
                run_id=run_id,
                target_type=target_type,
                target_id=target_id,
                current_hash=current_hash,
            )

    def find_equivalent_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        *,
        provider: str | None = None,
        model: str | None = None,
        model_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the completed equivalent assessment for this exact target."""
        _fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            return uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )

    def schedule_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an audit attempt or reuse one completed equivalent assessment."""
        fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        sanitized_manifest = (
            _sanitize(audit_packet_manifest) if audit_packet_manifest else None
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            existing = uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )
            if existing is not None:
                assessment_id = UUID(existing["id"])
                action = "reuse"
            else:
                assessment_id = uow.insert_audit_assessment_if_absent(
                    run_id=run_id,
                    target_type=target_type,
                    target_id=target_id,
                    target_hash=target_hash,
                    evaluator_version=evaluator_version,
                    prompt_template_version=prompt_template_version,
                    policy_version=policy_version,
                    stage_set=stage_set,
                    status=status,
                    audit_identity_hash=identity_hash,
                    provider=provider,
                    model=model,
                    prompt_hash=prompt_hash,
                    model_fingerprint=fingerprint,
                    elapsed_ms=elapsed_ms,
                    audit_packet_manifest=sanitized_manifest,
                )
                if assessment_id is None:
                    existing = uow.lookup_equivalent_assessment(
                        run_id, target_type, target_id, identity_hash
                    )
                    if existing is None:
                        raise RuntimeError(
                            "completed audit conflict did not resolve to an assessment"
                        )
                    assessment_id = UUID(existing["id"])
                    action = "reuse"
                else:
                    action = "create"

        export = self.export_assessment(assessment_id) or {}
        export["existing"] = action == "reuse"
        return {
            "action": action,
            "assessment_id": str(assessment_id),
            "audit_identity_hash": identity_hash,
            "existing": action == "reuse",
            "assessment": export,
        }

    def export_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        """Export a complete audit assessment with all stage outputs."""
        with self.uow_factory() as uow:
            return uow.export_audit_assessment(assessment_id)

    def assess_run(
        self,
        run_id: UUID,
        external_run_id: str,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule a target-scoped run assessment idempotently."""
        result = self.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            status=status,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            model_fingerprint=model_fingerprint,
            elapsed_ms=elapsed_ms,
            audit_packet_manifest=audit_packet_manifest,
        )
        assessment = dict(result["assessment"])
        assessment["external_run_id"] = external_run_id
        assessment["action"] = result["action"]
        return assessment

    def assess_invocation(
        self,
        run_id: UUID,
        invocation_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule a target-scoped invocation assessment idempotently."""
        result = self.schedule_assessment(
            run_id=run_id,
            target_type="invocation",
            target_id=invocation_id,
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            status=status,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            model_fingerprint=model_fingerprint,
            elapsed_ms=elapsed_ms,
            audit_packet_manifest=audit_packet_manifest,
        )
        assessment = dict(result["assessment"])
        assessment["invocation_id"] = str(invocation_id)
        assessment["action"] = result["action"]
        return assessment


__all__ = [
    "AUDIT_IDENTITY_VERSION",
    "AUDIT_MODEL_IMPLEMENTATION_VERSION",
    "AuditService",
    "compute_audit_identity_hash",
    "resolve_model_fingerprint",
]
