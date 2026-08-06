"""Explicit autonomous/curated run commands over PostgreSQL authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .asset_promotion_service import AssetPromotionService
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

    def seal_acquisition(self, external_run_id: str) -> dict[str, Any]:
        self._require_curated(external_run_id)
        status = self.workflow_service.seal_acquisition(
            external_run_id,
            idempotency_key=f"curated:seal-acquisition:{external_run_id}",
        )
        seal = self.promotion_service.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="operator",
            actor_identifier="frun",
            policy_version="curated-run-v1",
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
            idempotency_key=(
                f"curated:finish:{external_run_id}:{status_name}:{outcome}:"
                f"{source_manifest_sha256 or ''}:{answer_sha256 or ''}"
            ),
        )
        return RunModeStatus(status, "curated")
