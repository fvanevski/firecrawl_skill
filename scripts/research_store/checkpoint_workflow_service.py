"""Wrapper workflow adapter that finalizes sealed indexing checkpoints."""

from __future__ import annotations

import os
from typing import Any

from .index_checkpoint_service import IndexCheckpointService
from .workflow_service import WorkflowBoundaryError, WorkflowOperationService


class CheckpointWorkflowOperationService(WorkflowOperationService):
    """Preserve wrapper contracts while making checkpoint finalization mandatory."""

    def _checkpoint_service(self) -> IndexCheckpointService:
        return IndexCheckpointService(
            self.uow_factory,
            max_attempts=int(os.environ.get("MAX_INDEX_ATTEMPTS", "5")),
        )

    def _finalize_indexing(self, external_run_id: str, command_key: str) -> None:
        status = self.run_service.status(external_id=external_run_id)
        if status.state != "indexing":
            return
        service = self._checkpoint_service()
        checkpoint = service.ensure(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            fingerprint=service.active_fingerprint(status.id),
            idempotency_key=f"{command_key}:checkpoint",
        )
        result = service.finalize(
            status.id,
            checkpoint.id,
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"{command_key}:indexing-complete",
            actor_type="wrapper",
            actor_identifier="firecrawl-skill",
            reason="sealed run-scoped index census is complete",
        )
        if result.status == "recoverable":
            raise WorkflowBoundaryError(
                "run indexing remains recoverable; resume the checkpoint before "
                "continuing: " + str(result.census)
            )
        if result.status == "irrecoverable":
            raise WorkflowBoundaryError(
                "run indexing failed closed on irrecoverable census state: "
                + str(result.census)
            )
        if result.status == "invalidated":
            raise WorkflowBoundaryError(
                "run indexing checkpoint was invalidated: " + str(result.reason)
            )

    def begin_operation(
        self,
        external_run_id: str,
        external_invocation_id: str,
        operation: str,
        input_data: dict[str, Any],
    ):
        self._finalize_indexing(
            external_run_id,
            f"wrapper:{external_invocation_id}:resume",
        )
        return super().begin_operation(
            external_run_id,
            external_invocation_id,
            operation,
            input_data,
        )

    def finish_run(
        self,
        external_run_id: str,
        *,
        outcome: str,
        status_name: str = "complete",
        source_manifest_sha256: str | None = None,
        answer_sha256: str | None = None,
        idempotency_key: str | None = None,
    ):
        command_key = idempotency_key or (
            f"run:finish:{external_run_id}:{status_name}:{outcome}:"
            f"{source_manifest_sha256 or ''}:{answer_sha256 or ''}"
        )
        if status_name != "failed":
            self._finalize_indexing(external_run_id, command_key)
        return super().finish_run(
            external_run_id,
            outcome=outcome,
            status_name=status_name,
            source_manifest_sha256=source_manifest_sha256,
            answer_sha256=answer_sha256,
            idempotency_key=idempotency_key,
        )
