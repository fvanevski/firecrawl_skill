"""PostgreSQL-only workflow boundaries for command-line wrappers.

The high-level orchestrator owns its complete stage machine. Lower-level
``fsearch`` and ``fscrape`` wrappers use this service to register authoritative
invocations and to advance only the stages their work actually reaches.
Filesystem records are not read or written by this service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .invocation_service import InvocationError, InvocationRecord, InvocationService
from .run_service import ResearchRunService, RunStatus, TERMINAL_STATES


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
        accounted = self.pending + self.running + self.failed + self.dead + self.complete
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
    """Coordinate wrapper invocations with the PostgreSQL run state machine."""

    _BEGIN_PATHS = {
        "fsearch": {
            "created": ("planning", "corpus_review", "acquiring"),
            "planning": ("corpus_review", "acquiring"),
            "corpus_review": ("acquiring",),
            "coverage_review": ("acquiring",),
            "acquiring": (),
        },
        "fscrape": {
            "created": ("planning", "corpus_review", "acquiring", "extracting"),
            "planning": ("corpus_review", "acquiring", "extracting"),
            "corpus_review": ("acquiring", "extracting"),
            "coverage_review": ("extracting",),
            "acquiring": ("extracting",),
            "extracting": (),
        },
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

    def begin_operation(
        self,
        external_run_id: str,
        external_invocation_id: str,
        operation: str,
        input_data: dict[str, Any],
    ) -> InvocationRecord:
        if operation not in self._BEGIN_PATHS:
            raise WorkflowBoundaryError(f"unsupported wrapper operation: {operation}")
        status = self._status(external_run_id)
        if status.state == "indexing":
            progress = self.index_progress(status.id)
            if progress.assets == 0 or progress.complete == 0:
                raise WorkflowBoundaryError(
                    f"cannot begin {operation}: run {external_run_id} has no "
                    "completed indexed assets"
                )
            if progress.unfinished or progress.dead:
                raise WorkflowBoundaryError(
                    "cannot resume acquisition while indexing is incomplete: "
                    + str(progress.to_dict())
                )
            status = self._transition(
                status,
                "coverage_review",
                command_key=f"wrapper:{external_invocation_id}:resume",
                reason="completed indexing before additional acquisition",
            )
        path = self._BEGIN_PATHS[operation].get(status.state)
        if path is None:
            raise WorkflowBoundaryError(
                f"cannot begin {operation} while run {external_run_id} is "
                f"in state {status.state}"
            )
        command_key = f"wrapper:{external_invocation_id}:begin"
        status = self._advance_path(
            status,
            path,
            command_key=command_key,
            reason=f"begin {operation} wrapper operation",
        )
        return self.invocation_service.begin(
            status.id,
            external_invocation_id,
            operation,
            input_data,
            idempotency_key=f"{command_key}:invocation",
            actor_type="wrapper",
        )

    @staticmethod
    def _persisted_count(output: Any) -> int:
        if isinstance(output, list):
            return sum(bool(item.get("persisted")) for item in output if isinstance(item, dict))
        if isinstance(output, dict):
            if isinstance(output.get("records"), list):
                return WorkflowOperationService._persisted_count(output["records"])
            if isinstance(output.get("assets"), list):
                return sum(
                    item.get("status") == "complete"
                    for item in output["assets"]
                    if isinstance(item, dict)
                )
            value = output.get("persisted_count")
            if isinstance(value, int) and value >= 0:
                return value
        return 0

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
                    f"invocation {external_invocation_id} is already {invocation.status}"
                )
            completed = invocation
        else:
            try:
                completed = self.invocation_service.complete(
                    status.id,
                    invocation.id,
                    "succeeded" if succeeded else "failed",
                    output=(
                        output
                        if isinstance(output, dict)
                        else {"records": output or []}
                    ),
                    error=error,
                    actor_type="wrapper",
                )
            except InvocationError as exc:
                raise WorkflowBoundaryError(str(exc)) from exc

        if not succeeded or self._persisted_count(output) == 0:
            return completed

        status = self.run_service.status(run_id=status.id)
        path: tuple[str, ...]
        if status.state == "acquiring":
            path = ("extracting", "indexing")
        elif status.state == "extracting":
            path = ("indexing",)
        elif status.state in {"indexing", "coverage_review"}:
            path = ()
        else:
            raise WorkflowBoundaryError(
                f"successful persisted operation ended in unexpected state {status.state}"
            )
        self._advance_path(
            status,
            path,
            command_key=f"wrapper:{external_invocation_id}:complete",
            reason="wrapper persisted assets and queued vector indexing",
        )
        return completed

    def index_progress(self, run_id: UUID) -> RunIndexProgress:
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
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
    ) -> RunStatus:
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
            if current.state == "validating":
                pass
            current = self._transition(
                current,
                "failed",
                command_key=command_key,
                reason="operator declared run failure",
                outcome=outcome,
                error=outcome,
                completion={
                    "source_manifest_sha256": source_manifest_sha256,
                    "answer_sha256": answer_sha256,
                },
            )
            return current

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
                "complete a persisted acquisition first"
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
                },
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
            },
        )
