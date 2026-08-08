"""PostgreSQL-only workflow boundaries for command-line wrappers.

Direct ``fsearch`` and ``fscrape`` calls attach work only to a run that an
explicit lifecycle command has already prepared. Beginning or completing a
provider invocation never advances the run state implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from .invocation_service import InvocationError, InvocationRecord, InvocationService
from .run_service import TERMINAL_STATES, ResearchRunService, RunStatus


class WorkflowBoundaryError(RuntimeError):
    """A wrapper operation violates authoritative workflow policy."""


@dataclass(frozen=True)
class RunIndexProgress:
    assets: int
    chunks: int
    pending: int
    running: int
    failed: int
    dead: int
    complete: int

    @property
    def missing(self) -> int:
        accounted = (
            self.pending + self.running + self.failed + self.dead + self.complete
        )
        return max(0, self.chunks - accounted)

    @property
    def unfinished(self) -> int:
        return self.missing + self.pending + self.running + self.failed

    def to_dict(self) -> dict[str, int]:
        return {
            "assets": self.assets,
            "chunks": self.chunks,
            "missing": self.missing,
            "pending": self.pending,
            "running": self.running,
            "failed": self.failed,
            "dead": self.dead,
            "complete": self.complete,
        }


class WorkflowOperationService:
    """Coordinate explicit run commands and direct wrapper invocations."""

    _DIRECT_OPERATIONS: ClassVar[frozenset[str]] = frozenset({"fsearch", "fscrape"})
    _PREPARE_PATHS: ClassVar[dict[str, tuple[str, ...]]] = {
        "created": ("planning", "corpus_review", "acquiring"),
        "planning": ("corpus_review", "acquiring"),
        "corpus_review": ("acquiring",),
        "acquiring": (),
    }

    def __init__(
        self,
        run_service: ResearchRunService,
        invocation_service: InvocationService,
    ) -> None:
        self.run_service = run_service
        self.invocation_service = invocation_service
        self.uow_factory = run_service.uow_factory

    def _status(self, external_run_id: str) -> RunStatus:
        try:
            status = self.run_service.status(external_id=external_run_id)
        except KeyError as exc:
            raise WorkflowBoundaryError(
                f"research run {external_run_id!r} was not found"
            ) from exc
        if status.state in TERMINAL_STATES:
            raise WorkflowBoundaryError(
                f"research run {external_run_id} is terminal ({status.state}); "
                "reopen it before attaching new work"
            )
        return status

    def _transition(
        self,
        status: RunStatus,
        next_state: str,
        *,
        command_key: str,
        reason: str,
        outcome: str | None = None,
        error: str | None = None,
        completion: dict[str, Any] | None = None,
    ) -> RunStatus:
        self.run_service.transition(
            status.id,
            next_state,
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"{command_key}:to:{next_state}",
            actor_type="wrapper",
            actor_identifier="firecrawl-skill",
            triggering_event=f"run.wrapper.{next_state}",
            reason=reason,
            outcome=outcome,
            error=error,
            completion=completion,
        )
        return self.run_service.status(run_id=status.id)

    def _advance_path(
        self,
        status: RunStatus,
        states: tuple[str, ...],
        *,
        command_key: str,
        reason: str,
    ) -> RunStatus:
        current = status
        for next_state in states:
            current = self._transition(
                current,
                next_state,
                command_key=command_key,
                reason=reason,
            )
        return current

    def prepare_run(
        self,
        external_run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> RunStatus:
        """Explicitly move a new run to the sole direct-acquisition state."""
        status = self._status(external_run_id)
        path = self._PREPARE_PATHS.get(status.state)
        if path is None:
            raise WorkflowBoundaryError(
                f"cannot prepare run {external_run_id} from state {status.state}; "
                "finish or explicitly reopen the current lifecycle phase first"
            )
        command_key = idempotency_key or f"run:prepare:{external_run_id}"
        return self._advance_path(
            status,
            path,
            command_key=command_key,
            reason="operator explicitly prepared direct acquisition",
        )

    def seal_acquisition(
        self,
        external_run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> RunStatus:
        """Explicitly close direct acquisition and enter indexing."""
        status = self._status(external_run_id)
        command_key = idempotency_key or f"run:seal-acquisition:{external_run_id}"
        if status.state == "acquiring":
            status = self._transition(
                status,
                "extracting",
                command_key=command_key,
                reason="operator explicitly sealed direct acquisition",
            )
        if status.state == "extracting":
            status = self._transition(
                status,
                "indexing",
                command_key=command_key,
                reason="explicit acquisition set is ready for indexing",
            )
        if status.state != "indexing":
            raise WorkflowBoundaryError(
                f"cannot seal acquisition while run {external_run_id} is in "
                f"state {status.state}; prepare and complete acquisition first"
            )
        return status

    def begin_operation(
        self,
        external_run_id: str,
        external_invocation_id: str,
        operation: str,
        input_data: dict[str, Any],
    ) -> InvocationRecord:
        if operation not in self._DIRECT_OPERATIONS:
            raise WorkflowBoundaryError(f"unsupported wrapper operation: {operation}")
        if (
            not isinstance(external_invocation_id, str)
            or not external_invocation_id.strip()
        ):
            raise WorkflowBoundaryError("external_invocation_id is required")
        if not isinstance(input_data, dict):
            raise WorkflowBoundaryError("wrapper input_data must be an object")

        status = self._status(external_run_id)
        if status.state != "acquiring":
            raise WorkflowBoundaryError(
                f"cannot begin {operation} while run {external_run_id} is in "
                f"state {status.state}; run 'frun prepare {external_run_id}' "
                "before direct acquisition"
            )
        command_key = f"wrapper:{external_invocation_id}:begin"
        try:
            return self.invocation_service.begin(
                status.id,
                external_invocation_id,
                operation,
                input_data,
                idempotency_key=f"{command_key}:invocation",
                actor_type="wrapper",
            )
        except InvocationError as exc:
            raise WorkflowBoundaryError(str(exc)) from exc

    def complete_operation(
        self,
        external_run_id: str,
        external_invocation_id: str,
        *,
        succeeded: bool,
        output: Any = None,
        error: str | None = None,
    ) -> InvocationRecord:
        status = self._status(external_run_id)
        try:
            invocation = self.invocation_service.status(
                external_invocation_id=external_invocation_id
            )
        except KeyError as exc:
            raise WorkflowBoundaryError(
                f"invocation {external_invocation_id!r} was not found"
            ) from exc
        if invocation.run_id != status.id:
            raise WorkflowBoundaryError(
                f"invocation {external_invocation_id} belongs to another run"
            )
        desired = "complete" if succeeded else "failed"
        if invocation.status != "running":
            if invocation.status != desired:
                raise WorkflowBoundaryError(
                    f"invocation {external_invocation_id} is already "
                    f"{invocation.status}"
                )
            return invocation
        try:
            return self.invocation_service.complete(
                status.id,
                invocation.id,
                "succeeded" if succeeded else "failed",
                output=(
                    output if isinstance(output, dict) else {"records": output or []}
                ),
                error=error,
                actor_type="wrapper",
            )
        except InvocationError as exc:
            raise WorkflowBoundaryError(str(exc)) from exc

    def index_progress(self, run_id: UUID) -> RunIndexProgress:
        with self.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT count(DISTINCT ra.snapshot_id)
                       FROM research_run_assets ra
                       WHERE ra.run_id=%s""",
                (run_id,),
            )
            assets = int(cur.fetchone()[0])
            cur.execute(
                """SELECT count(DISTINCT c.id)
                       FROM research_run_assets ra
                       JOIN documents d ON d.snapshot_id=ra.snapshot_id
                       JOIN chunks c ON c.document_id=d.id
                       WHERE ra.run_id=%s
                         AND d.parser_version=%s
                         AND d.normalization_version=%s
                         AND c.chunker_version=%s""",
                (
                    run_id,
                    uow.parser_version,
                    uow.normalization_version,
                    uow.chunker_version,
                ),
            )
            chunks = int(cur.fetchone()[0])
            cur.execute(
                """SELECT
                         count(*) FILTER (WHERE j.status='pending'),
                         count(*) FILTER (WHERE j.status='running'),
                         count(*) FILTER (WHERE j.status='failed'),
                         count(*) FILTER (WHERE j.status='dead'),
                         count(*) FILTER (WHERE j.status='complete')
                       FROM research_run_assets ra
                       JOIN documents d ON d.snapshot_id=ra.snapshot_id
                       JOIN chunks c ON c.document_id=d.id
                       JOIN embedding_manifests m ON m.chunk_id=c.id
                       JOIN index_jobs j ON j.manifest_id=m.id
                       JOIN index_definitions idef ON idef.id=j.index_definition_id
                       WHERE ra.run_id=%s
                         AND d.parser_version=%s
                         AND d.normalization_version=%s
                         AND c.chunker_version=%s
                         AND idef.physical_collection=%s""",
                (
                    run_id,
                    uow.parser_version,
                    uow.normalization_version,
                    uow.chunker_version,
                    uow.index_name,
                ),
            )
            pending, running, failed, dead, complete = cur.fetchone()
        return RunIndexProgress(
            assets=assets,
            chunks=chunks,
            pending=int(pending),
            running=int(running),
            failed=int(failed),
            dead=int(dead),
            complete=int(complete),
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
        provenance_type: str | None = None,
    ) -> RunStatus:
        """Finish a run with authoritative synthesis provenance gates.

        Completion gates enforced for ``completed`` outcome:
        * ``answer_sha256`` and ``source_manifest_sha256`` must be populated.
        * A valid ``validation`` synthesis stage must exist.
        * No synthesis stage may be in a failed state.
        * ``provenance_type`` must be ``authoritative`` (or omitted) for
          completed runs; external/provisional outputs cannot satisfy the gate.

        Partial outcome is reserved for intentional policy-approved incomplete
        research and does not require synthesis provenance.
        """
        current = self.run_service.status(external_id=external_run_id)
        if current.state in TERMINAL_STATES:
            return current
        command_key = idempotency_key or (
            f"run:finish:{external_run_id}:{status_name}:{outcome}:"
            f"{source_manifest_sha256 or ''}:{answer_sha256 or ''}"
        )

        if status_name == "failed":
            if current.state == "created":
                current = self._transition(
                    current,
                    "planning",
                    command_key=command_key,
                    reason="initialize run before terminal failure",
                )
            return self._transition(
                current,
                "failed",
                command_key=command_key,
                reason="operator declared run failure",
                outcome=outcome,
                error=outcome,
                completion={
                    "source_manifest_sha256": source_manifest_sha256,
                    "answer_sha256": answer_sha256,
                    "provenance_type": provenance_type,
                },
            )

        progress = self.index_progress(current.id)
        if current.state in {
            "created",
            "planning",
            "corpus_review",
            "acquiring",
            "extracting",
        }:
            raise WorkflowBoundaryError(
                f"run {external_run_id} cannot finish from {current.state}; "
                "seal acquisition and complete indexing first"
            )
        if progress.assets == 0 or progress.complete == 0:
            raise WorkflowBoundaryError(
                f"run {external_run_id} has no completed indexed assets"
            )
        if progress.unfinished or progress.dead:
            raise WorkflowBoundaryError(
                "run indexing is not complete: " + str(progress.to_dict())
            )
        if current.state == "indexing":
            current = self._transition(
                current,
                "coverage_review",
                command_key=command_key,
                reason="all run-scoped index jobs completed",
            )

        if outcome == "partial":
            if current.state != "coverage_review":
                raise WorkflowBoundaryError(
                    f"partial completion requires coverage_review, got {current.state}"
                )
            return self._transition(
                current,
                "partial",
                command_key=command_key,
                reason="operator declared a partial result",
                outcome=outcome,
                completion={
                    "source_manifest_sha256": source_manifest_sha256,
                    "answer_sha256": answer_sha256,
                    "index_progress": progress.to_dict(),
                    "provenance_type": provenance_type,
                },
            )

        # --- Completion gates for authoritative completed outcome ----------
        self._assert_completion_gates(
            run_id=current.id,
            source_manifest_sha256=source_manifest_sha256,
            answer_sha256=answer_sha256,
            provenance_type=provenance_type,
        )

        if current.state == "coverage_review":
            current = self._transition(
                current,
                "synthesizing",
                command_key=command_key,
                reason="operator accepted coverage and began final synthesis",
            )
        if current.state == "synthesizing":
            current = self._transition(
                current,
                "validating",
                command_key=command_key,
                reason="operator supplied or accepted the final synthesis",
            )
        if current.state != "validating":
            raise WorkflowBoundaryError(
                f"complete outcome requires validating state, got {current.state}"
            )
        return self._transition(
            current,
            "completed",
            command_key=command_key,
            reason="operator completed the PostgreSQL-authoritative run",
            outcome=outcome,
            completion={
                "source_manifest_sha256": source_manifest_sha256,
                "answer_sha256": answer_sha256,
                "index_progress": progress.to_dict(),
                "provenance_type": provenance_type,
            },
        )

    def _assert_completion_gates(
        self,
        run_id: UUID,
        *,
        source_manifest_sha256: str | None,
        answer_sha256: str | None,
        provenance_type: str | None,
    ) -> None:
        """Validate synthesis provenance gates before allowing completion.

        Raises:
            WorkflowBoundaryError: When any gate is violated.
        """
        if not source_manifest_sha256:
            raise WorkflowBoundaryError(
                "completion requires source_manifest_sha256; "
                "populate the source manifest hash before completing"
            )
        if not answer_sha256:
            raise WorkflowBoundaryError(
                "completion requires answer_sha256; "
                "populate the answer hash before completing"
            )
        if provenance_type not in (None, "authoritative"):
            raise WorkflowBoundaryError(
                f"completion with provenance_type={provenance_type!r} is rejected; "
                "only 'authoritative' (or omitted) provenance satisfies the gate"
            )

        # Verify a valid validation synthesis stage exists and no stages failed.
        try:
            from .domain import SynthesisStageStatus
        except ImportError:  # pragma: no cover
            return
        uow_factory = self.run_service.uow_factory
        if uow_factory is None:
            return
        try:
            with uow_factory() as uow:
                stages = uow.get_synthesis_stages(run_id)
        except (RuntimeError, TypeError, AttributeError):  # pragma: no cover
            return
        if not stages:
            raise WorkflowBoundaryError(
                "completion requires a validation synthesis stage; "
                "persist the synthesis artifact before completing"
            )
        validation_stage = next(
            (s for s in stages if s.get("stage_name") == "validation"), None
        )
        if validation_stage is None:
            raise WorkflowBoundaryError(
                "completion requires a validation synthesis stage; "
                "persist the synthesis artifact before completing"
            )
        if validation_stage.get("stage_status") != "completed":
            raise WorkflowBoundaryError(
                "completion requires a valid (completed) validation synthesis stage; "
                f"got status={validation_stage.get('stage_status')!r}"
            )
        failed_stages = [
            s.get("stage_name")
            for s in stages
            if s.get("stage_status") == SynthesisStageStatus.FAILED
        ]
        if failed_stages:
            raise WorkflowBoundaryError(
                f"completion rejected due to irrecoverable failed synthesis stage(s): "
                f"{', '.join(failed_stages)}"
            )
