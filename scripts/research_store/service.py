from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any
from uuid import UUID

from .corpus_service import CorpusService, ParsedContent, PreparedIngest
from .domain import IngestRequest, IngestResult
from .invocation_events import _sanitize

__all__ = [
    "AuditService",
    "ClaimManifestService",
    "CorpusService",
    "IngestRequest",
    "IngestResult",
    "ParsedContent",
    "PreparedIngest",
    "compute_audit_identity_hash",
    "dumps",
    "json_default",
    "resolve_model_fingerprint",
]


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dumps(value) -> str:
    return json.dumps(value, indent=2, default=json_default)


class ClaimManifestService:
    """Authoritative service for research claims and evidence links.

    Persists claims and claim-to-passage evidence links in PostgreSQL.
    Validates all references before accepting. Rejects URL-only source
    resolution — callers must provide stable passage and snapshot IDs.
    """

    VALID_RELATIONSHIPS = frozenset({"supports", "contradicts", "qualifies", "context"})
    VALID_SEMANTIC_STATUSES = frozenset(
        {
            "supported",
            "contradicted",
            "qualified",
            "unsupported",
            "uncertain",
            "unassessed",
        }
    )

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory

    def create_claim(
        self,
        run_id: UUID,
        claim_id: UUID,
        statement: str,
        *,
        semantic_status: str = "unassessed",
        uncertainty: str | None = None,
        evidence_packet_revision: int = 1,
    ) -> UUID:
        """Insert or update a claim. Returns the row ``id``.

        Idempotent on ``(run_id, claim_id)``.
        """
        if not statement.strip():
            raise ValueError("claim statement must be non-empty")
        if semantic_status not in self.VALID_SEMANTIC_STATUSES:
            raise ValueError(f"invalid semantic_status: {semantic_status}")
        with self.uow_factory() as uow:
            row_id = uow.upsert_claim(
                run_id,
                claim_id,
                statement,
                semantic_status=semantic_status,
                uncertainty=uncertainty,
                evidence_packet_revision=evidence_packet_revision,
            )
        return row_id

    def create_evidence_link(
        self,
        run_id: UUID,
        claim_id: UUID,
        passage_id: UUID,
        snapshot_id: UUID,
        *,
        source_url: str = "",
        relationship: str = "supports",
        confidence: float = 1.0,
    ) -> UUID:
        """Insert a claim-evidence link. Returns the row ``id``.

        Validates that ``passage_id`` exists in ``chunks`` and
        ``snapshot_id`` exists in ``asset_snapshots`` before inserting.
        Rejects URL-only source references — the caller must provide
        stable ``passage_id`` and ``snapshot_id``.
        """
        if relationship not in self.VALID_RELATIONSHIPS:
            raise ValueError(f"invalid relationship: {relationship}")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        with self.uow_factory() as uow:
            row_id = uow.insert_evidence_link(
                run_id,
                claim_id,
                passage_id,
                snapshot_id,
                source_url=source_url,
                relationship=relationship,
                confidence=confidence,
            )
        return row_id

    def list_claims(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all claims for a run."""
        with self.uow_factory() as uow:
            return uow.list_claims(run_id)

    def list_evidence_links(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all evidence links for a run."""
        with self.uow_factory() as uow:
            return uow.list_evidence_links(run_id)

    def export_manifest(self, run_id: UUID) -> dict[str, Any]:
        """Export all claims and links for a run as a JSON-compatible dict."""
        with self.uow_factory() as uow:
            return uow.export_claim_manifest(run_id)

    def import_manifest(
        self,
        run_id: UUID,
        manifest: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Import claims and evidence links from a manifest dict.

        Dry-run-first: validates all references before committing.
        Idempotent — existing claims are upserted, links are appended.
        """
        claims = manifest.get("claims", [])
        links = manifest.get("links", [])

        # Dry-run phase: validate all references in a single UoW to avoid
        # opening O(n) connections (one per passage/snapshot check).
        unknown_passages = []
        unknown_snapshots = []
        malformed_claim_ids = []

        with self.uow_factory() as uow:
            for claim in claims:
                cid = claim.get("claim_id")
                if cid:
                    try:
                        UUID(str(cid))
                    except ValueError:
                        malformed_claim_ids.append(str(cid))

            for link in links:
                pid = link.get("passage_id")
                sid = link.get("snapshot_id")
                if pid:
                    try:
                        uid = UUID(str(pid))
                        if not uow.validate_passage_id(uid):
                            unknown_passages.append(str(pid))
                    except ValueError:
                        unknown_passages.append(str(pid))
                if sid:
                    try:
                        uid = UUID(str(sid))
                        if not uow.validate_snapshot_id(uid):
                            unknown_snapshots.append(str(sid))
                    except ValueError:
                        unknown_snapshots.append(str(sid))

        dry_run_result = {
            "dry_run": True,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "valid": not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

        if dry_run or (malformed_claim_ids or unknown_passages or unknown_snapshots):
            return dry_run_result

        # Apply phase: commit claims and links
        failed_claims = []
        failed_links = []
        inserted_claims = 0
        with self.uow_factory() as uow:
            for claim in claims:
                try:
                    uow.upsert_claim(
                        run_id,
                        UUID(str(claim["claim_id"])),
                        claim["statement"],
                        semantic_status=claim.get("semantic_status", "unassessed"),
                        uncertainty=claim.get("uncertainty"),
                        evidence_packet_revision=claim.get(
                            "evidence_packet_revision", 1
                        ),
                    )
                    inserted_claims += 1
                except Exception as exc:  # noqa: BLE001
                    failed_claims.append(
                        {
                            "claim_id": str(claim.get("claim_id", "unknown")),
                            "error": str(exc),
                        }
                    )

            inserted_links = 0
            for link in links:
                try:
                    uow.insert_evidence_link(
                        run_id,
                        UUID(str(link["claim_id"])),
                        UUID(str(link["passage_id"])),
                        UUID(str(link["snapshot_id"])),
                        source_url=link.get("source_url", ""),
                        relationship=link.get("relationship", "supports"),
                        confidence=link.get("confidence", 1.0),
                    )
                    inserted_links += 1
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc).lower()
                    if (
                        "unique constraint" in exc_str
                        or "uk_claim_evidence_links" in exc_str
                        or "duplicate key" in exc_str
                    ):
                        inserted_links += 1
                    else:
                        failed_links.append(
                            {
                                "claim_id": str(link.get("claim_id", "unknown")),
                                "passage_id": str(link.get("passage_id", "unknown")),
                                "error": str(exc),
                            }
                        )

        has_failures = bool(failed_claims) or bool(failed_links)
        return {
            "dry_run": False,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "inserted_claims": inserted_claims,
            "inserted_links": inserted_links,
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "failed_claims": failed_claims,
            "failed_links": failed_links,
            "valid": not has_failures
            and not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

    def _passage_id_valid(self, passage_id: UUID) -> bool:
        """Check if passage_id exists in chunks."""
        with self.uow_factory() as uow:
            return uow.validate_passage_id(passage_id)

    def _snapshot_id_valid(self, snapshot_id: UUID) -> bool:
        """Check if snapshot_id exists in asset_snapshots."""
        with self.uow_factory() as uow:
            return uow.validate_snapshot_id(snapshot_id)


# ---------------------------------------------------------------------------
# Audit service (issue #33)
# ---------------------------------------------------------------------------


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
    """Return a stable, non-empty evaluator/model fingerprint.

    Callers may supply a provider-issued fingerprint. Otherwise both provider
    and a fixed model identifier are required, and the service derives a
    deterministic fingerprint that also retains evaluator schema, prompt
    schema, and implementation versions.
    """
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
    """Recursively extract evidence reference IDs from a stage output dictionary/list structure."""
    refs: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_lower = str(k).lower()
            if k_lower in (
                "evidence_refs",
                "evidence_references",
                "claim_id",
                "claim_ids",
                "passage_id",
                "passage_ids",
                "snapshot_id",
                "snapshot_ids",
            ):
                if isinstance(v, (list, tuple, set)):
                    refs.extend([str(item) for item in v if item])
                elif v:
                    refs.append(str(v))
            else:
                refs.extend(_extract_evidence_references(v))
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
        """Add a stage output to an assessment. Returns the stage ``id``.

        Stage failures are recorded individually; successful stages remain
        intact. Evidence references in ``output`` are validated against the database.
        """
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
                            f"invalid evidence references in stage output: {sorted(set(invalid_refs))}"
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
        """Return assessments whose target_hash differs from current_hash.

        Stale assessments are retained as historical records.

        Validates that the target entity exists before querying.
        """
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
        """Append an audit attempt or reuse one completed equivalent assessment.

        Only completed assessments are reusable. Partial and failed rows remain
        immutable historical attempts and do not prevent a completed retry.
        """
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
