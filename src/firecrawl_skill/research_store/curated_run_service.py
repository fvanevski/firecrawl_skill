"""Explicit autonomous/curated run commands over PostgreSQL authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .acquisition.candidate_ranking import classify_url, is_generic_url_type
from .asset_promotion_service import AssetPromotionService
from .candidate_policy_service import decision_error_message
from .run_service import ResearchRunService, RunStatus
from .workflow_service import WorkflowBoundaryError, WorkflowOperationService

RUN_MODES = frozenset({"autonomous", "curated"})
LEGACY_RUN_MODE = "legacy_unspecified"


class CuratedRunError(WorkflowBoundaryError):
    """An explicit curated-run command violates run-mode policy."""


@dataclass(frozen=True)
class RunModeStatus:
    run: RunStatus
    run_mode: str

    def to_dict(self) -> dict[str, Any]:
        result = self.run.to_dict()
        result["run_mode"] = self.run_mode
        return result


class CuratedRunService:
    """Coordinate explicit direct-acquisition and curation commands.

    Run mode is stored in the existing authoritative ``research_runs.metadata``
    JSONB document. Historical rows are read as ``legacy_unspecified``; this
    service never backfills or infers a historical mode.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        workflow_service: WorkflowOperationService,
        promotion_service: AssetPromotionService | Any,
    ) -> None:
        self.run_service = run_service
        self.workflow_service = workflow_service
        self.promotion_service = promotion_service
        self.uow_factory = run_service.uow_factory

    @staticmethod
    def _validate_run_mode(run_mode: str) -> str:
        normalized = run_mode.strip().lower()
        if normalized not in RUN_MODES:
            raise ValueError(
                f"unsupported run mode {run_mode!r}; expected autonomous or curated"
            )
        return normalized

    def start(
        self,
        objective: str,
        external_id: str,
        *,
        run_mode: str,
        execution_mode: str = "autonomous_local",
        idempotency_key: str | None = None,
    ) -> RunModeStatus:
        normalized_mode = self._validate_run_mode(run_mode)
        status = self.run_service.create(
            objective,
            external_id,
            execution_mode=execution_mode,
            idempotency_key=idempotency_key,
            actor_type="cli",
            actor_identifier="frun",
            metadata={"run_mode": normalized_mode},
        )
        return RunModeStatus(status, self.mode(status.id))

    def mode(self, run_id: UUID) -> str:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                "SELECT metadata->>'run_mode' FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        value = row[0]
        if value is None:
            return LEGACY_RUN_MODE
        normalized = str(value)
        if normalized not in RUN_MODES:
            raise CuratedRunError(
                f"run {run_id} has unsupported persisted run mode {normalized!r}"
            )
        return normalized

    def status(self, external_run_id: str) -> RunModeStatus:
        status = self.run_service.status(external_id=external_run_id)
        return RunModeStatus(status, self.mode(status.id))

    def _require_curated(self, external_run_id: str) -> RunStatus:
        mode_status = self.status(external_run_id)
        if mode_status.run_mode != "curated":
            raise CuratedRunError(
                f"run {external_run_id} is {mode_status.run_mode}, not curated; "
                "assets, retain, reject, and seal-acquisition are curated-only "
                "commands"
            )
        return mode_status.run

    def prepare(
        self,
        external_run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> RunModeStatus:
        status = self.workflow_service.prepare_run(
            external_run_id,
            idempotency_key=idempotency_key,
        )
        return RunModeStatus(status, self.mode(status.id))

    def assets(self, external_run_id: str) -> dict[str, Any]:
        """Return authoritative promotion subjects for one curated run."""
        status = self._require_curated(external_run_id)
        assets = self.promotion_service.list_assets(status.id)
        return {
            "run_id": str(status.id),
            "external_id": status.external_id,
            "state": status.state,
            "lifecycle_revision": status.lifecycle_revision,
            "run_mode": "curated",
            "asset_count": len(assets),
            "assets": assets,
        }

    def retain(
        self,
        external_run_id: str,
        subject_id: UUID,
        *,
        reason: str = "operator retained asset for curated completion",
    ) -> dict[str, Any]:
        status = self._require_curated(external_run_id)
        if status.state != "acquiring":
            raise CuratedRunError(
                f"cannot retain assets while run {external_run_id} is in "
                f"state {status.state}; retain or reject before seal-acquisition"
            )
        return self.promotion_service.promote(
            subject_id,
            "retained",
            expected_lifecycle_revision=status.lifecycle_revision,
            expected_run_id=status.id,
            actor_type="operator",
            actor_identifier="frun",
            policy_version="curated-run-v1",
            reason_code="curated_asset_retained",
            reason=reason,
        )

    def reject(
        self,
        external_run_id: str,
        subject_id: UUID,
        *,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("rejection reason is required")
        status = self._require_curated(external_run_id)
        if status.state != "acquiring":
            raise CuratedRunError(
                f"cannot reject assets while run {external_run_id} is in "
                f"state {status.state}; retain or reject before seal-acquisition"
            )
        return self.promotion_service.reject(
            subject_id,
            expected_lifecycle_revision=status.lifecycle_revision,
            expected_run_id=status.id,
            actor_type="operator",
            actor_identifier="frun",
            policy_version="curated-run-v1",
            reason_code="curated_asset_rejected",
            reason=reason,
        )

    @staticmethod
    def _preview_digest(
        run_id: UUID,
        lifecycle_revision: int,
        metrics: dict[str, Any],
        scope: dict[str, Any],
        budget: dict[str, Any],
        hard_violations: list[dict[str, Any]],
        soft_violations: list[dict[str, Any]],
    ) -> str:
        payload = {
            "schema_version": "candidate-budget-check-v1",
            "run_id": str(run_id),
            "phase": "completion_admission",
            "lifecycle_revision": lifecycle_revision,
            "metrics": metrics,
            "scope": scope,
            "budget": budget,
            "hard_violations": hard_violations,
            "soft_violations": soft_violations,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    def _completion_preview_material(
        self,
        uow: Any,
        cursor: Any,
        run_id: UUID,
        *,
        stages: tuple[str, ...],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Remeasure one prospective completion set on the caller's run lock."""
        cursor.execute(
            """SELECT subject.id,COALESCE(snapshot.raw_byte_length,0),
                      COALESCE(candidate.canonical_url,source.canonical_url),
                      count(DISTINCT chunk.id)
                 FROM run_asset_promotion_subjects subject
                 JOIN asset_snapshots snapshot ON snapshot.id=subject.snapshot_id
                 JOIN sources source ON source.id=snapshot.source_id
                 LEFT JOIN search_candidates candidate ON candidate.id=subject.candidate_id
                 LEFT JOIN documents document ON document.snapshot_id=snapshot.id
                      AND document.parser_version=%s
                      AND document.normalization_version=%s
                 LEFT JOIN chunks chunk ON chunk.document_id=document.id
                      AND chunk.chunker_version=%s
                WHERE subject.run_id=%s AND subject.current_stage = ANY(%s)
                GROUP BY subject.id,snapshot.raw_byte_length,
                         candidate.canonical_url,source.canonical_url""",
            (
                uow.parser_version,
                uow.normalization_version,
                uow.chunker_version,
                run_id,
                list(stages),
            ),
        )
        rows = cursor.fetchall()
        per_asset_chunk_counts = {str(row[0]): int(row[3] or 0) for row in rows}
        cursor.execute(
            "SELECT count(*) FROM extraction_attempts WHERE run_id=%s", (run_id,)
        )
        extraction_attempts = int(cursor.fetchone()[0] or 0)
        metrics = {
            "candidate_count": len(rows),
            "total_bytes": sum(int(row[1] or 0) for row in rows),
            "total_chunks": sum(per_asset_chunk_counts.values()),
            "generic_page_count": sum(
                is_generic_url_type(classify_url(str(row[2] or ""))) for row in rows
            ),
            "extraction_attempts": extraction_attempts,
            "per_asset_chunk_counts": dict(sorted(per_asset_chunk_counts.items())),
        }
        scope = {"subject_ids": sorted(per_asset_chunk_counts)}
        budget = dict(self.promotion_service.candidate_budget.to_dict())
        return metrics, scope, budget

    @staticmethod
    def _preview_row(cursor: Any, run_id: UUID, check_id: UUID) -> dict[str, Any]:
        cursor.execute(
            """SELECT id,lifecycle_revision,candidate_count,total_bytes,total_chunks,
                      generic_page_count,extraction_attempts,per_asset_chunk_counts,
                      scope,budget,hard_violations,soft_violations,
                      accepted_without_override,content_sha256
                 FROM corpus_budget_checks
                WHERE id=%s AND run_id=%s AND phase='completion_admission'
                FOR SHARE""",
            (check_id, run_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise CuratedRunError(
                f"completion-admission preview {check_id} does not belong to run {run_id}"
            )
        names = (
            "id",
            "lifecycle_revision",
            "candidate_count",
            "total_bytes",
            "total_chunks",
            "generic_page_count",
            "extraction_attempts",
            "per_asset_chunk_counts",
            "scope",
            "budget",
            "hard_violations",
            "soft_violations",
            "accepted_without_override",
            "content_sha256",
        )
        return dict(zip(names, row, strict=True))

    @staticmethod
    def _preview_overrides(cursor: Any, check_id: UUID) -> frozenset[str]:
        cursor.execute(
            """SELECT DISTINCT limit_name FROM budget_override_justifications
                WHERE budget_check_id=%s""",
            (check_id,),
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())

    def _preview_matches_material(
        self,
        cursor: Any,
        run_id: UUID,
        check: dict[str, Any],
        *,
        lifecycle_revision: int,
        metrics: dict[str, Any],
        scope: dict[str, Any],
        budget: dict[str, Any],
    ) -> bool:
        if check["lifecycle_revision"] is None:
            return False
        if int(check["lifecycle_revision"]) != lifecycle_revision:
            return False
        stored_metrics = {
            "candidate_count": int(check["candidate_count"]),
            "total_bytes": int(check["total_bytes"]),
            "total_chunks": int(check["total_chunks"]),
            "generic_page_count": int(check["generic_page_count"]),
            "extraction_attempts": int(check["extraction_attempts"]),
            "per_asset_chunk_counts": dict(
                sorted(dict(check["per_asset_chunk_counts"] or {}).items())
            ),
        }
        stored_scope = dict(check["scope"] or {})
        stored_budget = dict(check["budget"] or {})
        hard_violations = list(check["hard_violations"] or [])
        soft_violations = list(check["soft_violations"] or [])
        if stored_metrics != metrics or stored_scope != scope or stored_budget != budget:
            return False
        digest = self._preview_digest(
            run_id,
            lifecycle_revision,
            metrics,
            scope,
            budget,
            hard_violations,
            soft_violations,
        )
        if digest != str(check["content_sha256"]):
            return False
        if hard_violations:
            return False
        soft_limits = {
            str(item.get("limit_name"))
            for item in soft_violations
            if item.get("limit_name") is not None
        }
        overrides = self._preview_overrides(cursor, UUID(str(check["id"])))
        return soft_limits <= overrides

    def _assert_locked_preview_matches(
        self,
        uow: Any,
        cursor: Any,
        run_id: UUID,
        check_id: UUID,
        lifecycle_revision: int,
    ) -> None:
        metrics, scope, budget = self._completion_preview_material(
            uow,
            cursor,
            run_id,
            stages=("retained",),
        )
        check = self._preview_row(cursor, run_id, check_id)
        if not self._preview_matches_material(
            cursor,
            run_id,
            check,
            lifecycle_revision=lifecycle_revision,
            metrics=metrics,
            scope=scope,
            budget=budget,
        ):
            raise CuratedRunError(
                "completion-admission preview changed before acquisition seal; "
                "run remains acquiring so the operator can re-curate and retry"
            )

    def _commit_preview_guarded_extracting(
        self,
        external_run_id: str,
        status: RunStatus,
        preview_check_id: UUID,
    ) -> RunStatus:
        """Revalidate preview and commit acquiring->extracting on one run lock."""
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, revision = uow.runs._lock_workflow_run(cursor, status.id)
            if state != "acquiring" or int(revision) != status.lifecycle_revision:
                raise CuratedRunError(
                    "completion-admission preview is stale because the run lifecycle "
                    f"changed: expected acquiring@{status.lifecycle_revision}, "
                    f"got {state}@{revision}"
                )
            self._assert_locked_preview_matches(
                uow,
                cursor,
                status.id,
                preview_check_id,
                status.lifecycle_revision,
            )
            try:
                uow.runs.apply_run_transition(
                    status.id,
                    "extracting",
                    status.lifecycle_revision,
                    f"curated:seal-acquisition:{external_run_id}:to:extracting",
                    "wrapper",
                    self.run_service.policy_version,
                    permitted_prior_states=frozenset({"acquiring"}),
                    actor_identifier="firecrawl-skill",
                    event_type="run.wrapper.extracting",
                    reason="operator explicitly sealed direct acquisition",
                    completion={},
                )
            except ValueError as exc:
                raise CuratedRunError(str(exc)) from exc
        return self.run_service.status(run_id=status.id)

    def _recover_predecessor_preview(self, status: RunStatus) -> UUID | None:
        """Recover an exact accepted acquiring preview for interrupted sealing.

        The recovery path is used only after the canonical acquiring->extracting
        ->indexing pair is already durable. It remeasures the same prospective
        membership across all in-progress sealing stages and returns a preview
        only when the predecessor revision, metrics, scope, budget, violations,
        and soft overrides are all exact.
        """
        if status.state != "indexing" or status.lifecycle_revision < 2:
            return None
        predecessor_revision = status.lifecycle_revision - 2
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, revision = uow.runs._lock_workflow_run(cursor, status.id)
            if state != "indexing" or int(revision) != status.lifecycle_revision:
                raise CuratedRunError(
                    "run lifecycle changed while recovering completion admission"
                )
            cursor.execute(
                """SELECT lifecycle_revision,prior_state,next_state
                     FROM research_run_transitions
                    WHERE run_id=%s AND lifecycle_revision = ANY(%s)
                    ORDER BY lifecycle_revision""",
                (
                    status.id,
                    [status.lifecycle_revision - 1, status.lifecycle_revision],
                ),
            )
            transitions = [
                (int(row[0]), str(row[1]), str(row[2])) for row in cursor.fetchall()
            ]
            expected = [
                (status.lifecycle_revision - 1, "acquiring", "extracting"),
                (status.lifecycle_revision, "extracting", "indexing"),
            ]
            if transitions != expected:
                return None

            metrics, scope, budget = self._completion_preview_material(
                uow,
                cursor,
                status.id,
                stages=("retained", "evidence_eligible", "completion_critical"),
            )
            cursor.execute(
                """SELECT id FROM corpus_budget_checks
                    WHERE run_id=%s AND phase='completion_admission'
                      AND lifecycle_revision=%s
                    ORDER BY created_at,id""",
                (status.id, predecessor_revision),
            )
            matches: list[UUID] = []
            for (check_id,) in cursor.fetchall():
                resolved = UUID(str(check_id))
                check = self._preview_row(cursor, status.id, resolved)
                if self._preview_matches_material(
                    cursor,
                    status.id,
                    check,
                    lifecycle_revision=predecessor_revision,
                    metrics=metrics,
                    scope=scope,
                    budget=budget,
                ):
                    matches.append(resolved)
            if len(matches) > 1:
                raise CuratedRunError(
                    "multiple exact predecessor completion-admission previews matched; "
                    "repair fails closed"
                )
            return matches[0] if matches else None

    def seal_acquisition(self, external_run_id: str) -> dict[str, Any]:
        status = self._require_curated(external_run_id)
        preview_check_id: UUID | None = None
        preview_revision: int | None = None
        if status.state == "acquiring":
            preview = self.promotion_service.candidate_policy_service.evaluate_completion_admission_preview(
                status.id,
                status.lifecycle_revision,
                self.promotion_service.candidate_budget,
            )
            if not preview.accepted:
                raise CuratedRunError(decision_error_message(preview))
            preview_check_id = preview.check_id
            preview_revision = status.lifecycle_revision
            status = self._commit_preview_guarded_extracting(
                external_run_id,
                status,
                preview_check_id,
            )

        status = self.workflow_service.seal_acquisition(
            external_run_id,
            idempotency_key=f"curated:seal-acquisition:{external_run_id}",
        )
        if preview_check_id is not None:
            if preview_revision is None or status.lifecycle_revision != preview_revision + 2:
                raise CuratedRunError(
                    "completion-admission preview is not the exact acquiring "
                    "predecessor of the indexing revision; override carry-forward "
                    "is refused"
                )
        elif status.state == "indexing":
            preview_check_id = self._recover_predecessor_preview(status)

        seal = self.promotion_service.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="operator",
            actor_identifier="frun",
            policy_version="curated-run-v1",
            completion_admission_preview_id=preview_check_id,
        )
        return {
            "run_id": str(status.id),
            "external_id": status.external_id,
            "state": status.state,
            "lifecycle_revision": status.lifecycle_revision,
            "run_mode": "curated",
            "seal_id": str(seal.id),
            "seal_revision": seal.seal_revision,
            "membership_sha256": seal.membership_sha256,
            "expected_asset_count": seal.expected_asset_count,
            "expected_chunk_count": seal.expected_chunk_count,
        }

    def resume(self, external_run_id: str) -> dict[str, Any]:
        mode_status = self.status(external_run_id)
        status = mode_status.run
        membership_sealed: bool | None = None
        if mode_status.run_mode != "curated":
            return {
                **mode_status.to_dict(),
                "next_action": "resume autonomous index checkpoint",
            }
        if status.state in {"created", "planning", "corpus_review"}:
            next_action = f"frun prepare {external_run_id}"
        elif status.state in {"acquiring", "extracting"}:
            next_action = f"frun seal-acquisition {external_run_id}"
        elif status.state == "indexing":
            membership_sealed = (
                self.promotion_service.get_active_seal(status.id) is not None
            )
            next_action = (
                "resume index checkpoint"
                if membership_sealed
                else f"frun seal-acquisition {external_run_id}"
            )
        elif status.state in {"coverage_review", "synthesizing", "validating"}:
            next_action = f"frun finish {external_run_id} --outcome satisfied"
        else:
            next_action = "none"
        result = {**mode_status.to_dict(), "next_action": next_action}
        if membership_sealed is not None:
            result["membership_sealed"] = membership_sealed
        return result

    def finish(
        self,
        external_run_id: str,
        *,
        outcome: str,
        status_name: str = "complete",
        source_manifest_sha256: str | None = None,
        answer_sha256: str | None = None,
        provenance_type: str | None = None,
    ) -> RunModeStatus:
        current = self._require_curated(external_run_id)
        if (
            status_name != "failed"
            and current.state == "indexing"
            and self.promotion_service.get_active_seal(current.id) is None
        ):
            raise CuratedRunError(
                f"run {external_run_id} has no active completion membership; "
                f"run 'frun seal-acquisition {external_run_id}' before finish"
            )
        status = self.workflow_service.finish_run(
            external_run_id,
            outcome=outcome,
            status_name=status_name,
            source_manifest_sha256=source_manifest_sha256,
            answer_sha256=answer_sha256,
            provenance_type=provenance_type,
            idempotency_key=(
                f"curated:finish:{external_run_id}:{status_name}:{outcome}:"
                f"{source_manifest_sha256 or ''}:{answer_sha256 or ''}"
            ),
        )
        return RunModeStatus(status, "curated")
