"""PostgreSQL-backed invocation catalog service.

This module provides the authoritative invocation catalog API backed
by PostgreSQL.  It maps the filesystem-based Catalog v5 operations
(``begin``, ``add_event``, ``complete``, etc.) to PostgreSQL
``research_invocations`` and ``research_events`` tables.

PRD mapping: FR-001, Section 14

Authority model:

* PostgreSQL ``research_invocations`` and ``research_events`` are
  authoritative.
* Filesystem Catalog v5 records are derived compatibility exports,
  written *after* database commit.
* Filesystem records are never read to determine current invocation
  or run state.

Event types supported (mapped from Catalog v5):

* ``invocation_started`` — invocation began
* ``invocation_finished`` — invocation completed
* ``invocation_event`` — generic invocation event (pivot, retry, etc.)
* ``pivot`` — search pivot to new query
* ``retry`` — retry of a failed operation
* ``decision`` — deterministic decision
* ``recovery`` — recovery from transient failure
* ``annotation`` — human or agent annotation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from .invocation_events import (
    EventAppendResult,
    EventService,
    InvocationEvent,
    _sanitize,
)


def utcnow() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class InvocationRecord:
    """Immutable representation of an invocation record.

    Attributes:
        id: Invocation UUID.
        run_id: Associated research run.
        parent_invocation_id: Parent invocation (for nested operations).
        external_invocation_id: External identifier (e.g. ``fc_<uuid>``).
        operation: Operation type (search, scrape, smart_search).
        status: Current status.
        lifecycle_revision: Run lifecycle revision at invocation start.
        input: Sanitized input data.
        output: Output data (populated on completion).
        error: Error message (if failed).
        metadata: Additional metadata.
        started_at: When the invocation started.
        completed_at: When the invocation completed.
        created_at: When the record was created.
    """

    id: UUID
    run_id: UUID
    parent_invocation_id: UUID | None
    external_invocation_id: str | None
    operation: str
    status: str
    lifecycle_revision: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    metadata: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "InvocationRecord":
        return cls(
            id=value["id"],
            run_id=value["run_id"],
            parent_invocation_id=value.get("parent_invocation_id"),
            external_invocation_id=value.get("external_invocation_id"),
            operation=value["operation"],
            status=value["status"],
            lifecycle_revision=value["lifecycle_revision"],
            input=value.get("input", {}),
            output=value.get("output"),
            error=value.get("error"),
            metadata=value.get("metadata", {}),
            started_at=value.get("started_at"),
            completed_at=value.get("completed_at"),
            created_at=value["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary suitable for compatibility export."""
        return {
            "invocation_id": str(self.id),
            "external_invocation_id": self.external_invocation_id,
            "run_id": str(self.run_id),
            "operation": self.operation,
            "status": self.status,
            "lifecycle_revision": self.lifecycle_revision,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class InvocationCatalogError(Exception):
    """Base exception for invocation catalog errors."""


class InvocationAlreadyRunning(InvocationCatalogError):
    """Raised when attempting to start an already-running invocation."""


class InvocationNotFound(InvocationCatalogError):
    """Raised when an invocation ID is not found."""


class InvocationCatalogService:
    """PostgreSQL-backed invocation catalog service.

    This service provides the authoritative API for managing
    invocations and their events.  It is the primary interface
    for the new PostgreSQL-backed invocation catalog.

    Args:
        uow_factory: Callable that returns a ``PostgresUnitOfWork``.
        event_service: ``EventService`` instance for event management.
    """

    def __init__(
        self,
        uow_factory: Callable,
        event_service: EventService | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.event_service = event_service or EventService(uow_factory)

    def begin(
        self,
        run_id: UUID,
        external_invocation_id: str,
        operation: str,
        input_data: dict[str, Any],
        *,
        parent_invocation_id: UUID | None = None,
        idempotency_key: str | None = None,
        actor_type: str = "system",
    ) -> InvocationRecord:
        """Begin a new invocation for a research run.

        This is the PostgreSQL-backed equivalent of Catalog v5's
        ``begin()`` function.

        Args:
            run_id: Research run UUID.
            external_invocation_id: External identifier (e.g. ``fc_<uuid>``).
            operation: Operation type (search, scrape, smart_search).
            input_data: Sanitized input data.
            parent_invocation_id: Optional parent invocation for nested ops.
            idempotency_key: Optional deduplication key.
            actor_type: Actor type for the invocation-started event.

        Returns:
            The created ``InvocationRecord``.

        Raises:
            ValueError: If required arguments are empty or malformed.
        """
        if not run_id:
            raise ValueError("run_id is required")
        if not external_invocation_id.strip():
            raise ValueError("external_invocation_id is required")
        if not operation.strip():
            raise ValueError("operation is required")

        sanitized_input = _sanitize(input_data)
        idempotency_key = idempotency_key or f"invocation:begin:{external_invocation_id}"

        with self.uow_factory() as uow:
            # Check for existing running invocation with same external ID
            cur = uow.connection.cursor()
            cur.execute(
                """SELECT id, status FROM research_invocations
                WHERE external_invocation_id = %s""",
                (external_invocation_id,),
            )
            existing = cur.fetchone()
            if existing:
                invocation_id, status = existing
                if status == "running":
                    raise InvocationAlreadyRunning(
                        f"invocation {external_invocation_id} is already running"
                    )

            # Get current run lifecycle revision
            cur.execute(
                "SELECT lifecycle_revision FROM research_runs WHERE id = %s",
                (run_id,),
            )
            run_row = cur.fetchone()
            if run_row is None:
                raise KeyError(f"run {run_id} not found")
            revision = run_row[0]

            # Insert invocation record
            cur.execute(
                """INSERT INTO research_invocations(
                run_id, parent_invocation_id, external_invocation_id,
                operation, status, lifecycle_revision, idempotency_key,
                input, metadata)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
                (
                    run_id,
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    "running",
                    revision,
                    idempotency_key,
                    sanitized_input,
                    {"actor_type": actor_type},
                ),
            )
            invocation_id = cur.fetchone()[0]

            # Append invocation_started event
            uow.runs.append_event(
                run_id,
                "invocation_started",
                actor_type,
                f"invocation:started:{external_invocation_id}",
                invocation_id=invocation_id,
                payload={
                    "operation": operation,
                    "external_invocation_id": external_invocation_id,
                },
            )

            # Fetch and return the record
            return InvocationRecord.from_mapping(
                uow.runs.get_invocation_status(invocation_id=invocation_id)
            )

    def add_event(
        self,
        run_id: UUID,
        invocation_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        actor_type: str = "system",
    ) -> EventAppendResult:
        """Append an event to an invocation.

        Args:
            run_id: Research run UUID.
            invocation_id: Invocation UUID.
            event_type: Constrained event type.
            payload: Sanitized event payload.
            idempotency_key: Optional deduplication key.
            actor_type: Actor type for the event.

        Returns:
            ``EventAppendResult`` with event metadata.
        """
        idempotency_key = idempotency_key or f"event:{event_type}:{uuid4()}"
        return self.event_service.append(
            run_id,
            event_type,
            actor_type,
            idempotency_key,
            invocation_id=invocation_id,
            payload=payload,
        )

    def complete(
        self,
        run_id: UUID,
        invocation_id: UUID,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        actor_type: str = "system",
    ) -> InvocationRecord:
        """Complete an invocation.

        Args:
            run_id: Research run UUID.
            invocation_id: Invocation UUID.
            status: Terminal status (succeeded, failed).
            output: Output data.
            error: Error message (if failed).
            actor_type: Actor type for the invocation_finished event.

        Returns:
            The updated ``InvocationRecord``.

        Raises:
            KeyError: If the invocation is not found.
        """
        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute(
                """SELECT id, run_id, operation, status, lifecycle_revision,
                        input, started_at
                 FROM research_invocations
                 WHERE id = %s""",
                (invocation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"invocation {invocation_id} not found")

            _, inv_run_id, operation, current_status, revision, input_data, started_at = row
            if current_status != "running":
                raise InvocationCatalogError(
                    f"invocation {invocation_id} is not running (status={current_status})"
                )

            completed_at = utcnow()
            duration_ms = None
            if started_at:
                delta = completed_at - started_at
                duration_ms = int(delta.total_seconds() * 1000)

            # Update invocation record
            db_status = "complete" if status == "succeeded" else "failed"
            cur.execute(
                """UPDATE research_invocations
                SET status = %s, completed_at = %s, output = %s, error = %s,
                    metadata = metadata || jsonb_build_object(
                        'terminal_status', %s,
                        'duration_ms', %s
                    )
                WHERE id = %s AND run_id = %s
                RETURNING id, run_id, parent_invocation_id, external_invocation_id,
                    operation, status, lifecycle_revision, input, output, error,
                    metadata, started_at, completed_at, created_at""",
                (
                    db_status,
                    completed_at,
                    _sanitize(output or {}),
                    error,
                    status,
                    duration_ms,
                    invocation_id,
                    run_id,
                ),
            )
            record_row = cur.fetchone()
            keys = (
                "id", "run_id", "parent_invocation_id", "external_invocation_id",
                "operation", "status", "lifecycle_revision", "input", "output",
                "error", "metadata", "started_at", "completed_at", "created_at",
            )
            record = InvocationRecord.from_mapping(dict(zip(keys, record_row)))

            # Append invocation_finished event
            uow.runs.append_event(
                run_id,
                "invocation_finished",
                actor_type,
                f"invocation:finished:{invocation_id}",
                invocation_id=invocation_id,
                payload={
                    "operation": operation,
                    "terminal_status": status,
                    "duration_ms": duration_ms,
                },
            )

            return record

    def status(
        self,
        *,
        run_id: UUID | None = None,
        invocation_id: UUID | None = None,
        external_invocation_id: str | None = None,
    ) -> InvocationRecord:
        """Get the current status of an invocation.

        Args:
            run_id: Optional run filter.
            invocation_id: Optional invocation UUID.
            external_invocation_id: Optional external identifier.

        Returns:
            The ``InvocationRecord``.

        Raises:
            KeyError: If the invocation is not found.
        """
        with self.uow_factory() as uow:
            return InvocationRecord.from_mapping(
                uow.runs.get_invocation_status(
                    run_id=run_id,
                    invocation_id=invocation_id,
                    external_invocation_id=external_invocation_id,
                )
            )

    def list_invocations(
        self,
        run_id: UUID,
        *,
        operation: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InvocationRecord]:
        """List invocations for a run.

        Args:
            run_id: Research run UUID.
            operation: Optional operation filter.
            status: Optional status filter.
            limit: Maximum invocations to return.
            offset: Pagination offset.

        Returns:
            List of ``InvocationRecord`` objects.
        """
        with self.uow_factory() as uow:
            rows = uow.runs.list_invocations(
                run_id,
                operation=operation,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [InvocationRecord.from_mapping(row) for row in rows]

    def list_events(
        self,
        run_id: UUID,
        *,
        invocation_id: UUID | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InvocationEvent]:
        """List events for a run or invocation.

        Args:
            run_id: Research run UUID.
            invocation_id: Optional invocation filter.
            event_type: Optional event type filter.
            limit: Maximum events to return.
            offset: Pagination offset.

        Returns:
            List of ``InvocationEvent`` objects.
        """
        return self.event_service.list_events(
            run_id,
            invocation_id=invocation_id,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

    def get_event(
        self, run_id: UUID, event_id: UUID
    ) -> InvocationEvent:
        """Retrieve a single event by ID.

        Args:
            run_id: Research run UUID.
            event_id: Event UUID.

        Returns:
            ``InvocationEvent`` for the requested event.

        Raises:
            KeyError: If the event does not exist.
        """
        return self.event_service.get_event(run_id, event_id)

    def export_to_catalog_format(
        self,
        run_id: UUID,
        invocation_id: UUID,
    ) -> dict[str, Any]:
        """Export an invocation record to Catalog v5-compatible format.

        This is a derived compatibility export, written *after*
        database commit.  It is NOT authoritative.

        Args:
            run_id: Research run UUID.
            invocation_id: Invocation UUID.

        Returns:
            A dictionary in Catalog v5 format.
        """
        record = self.status(invocation_id=invocation_id)
        events = self.list_events(run_id, invocation_id=invocation_id)

        catalog_record = {
            "schema_version": 5,
            "invocation_id": str(invocation_id),
            "external_invocation_id": record.external_invocation_id,
            "research_run_id": str(run_id),
            "operation": record.operation,
            "input": record.input,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.completed_at.isoformat() if record.completed_at else None,
            "execution": {
                "status": "succeeded" if record.status == "complete" else "failed",
                "exit_code": None,
                "error": record.error,
            },
            "operational_status": record.status,
            "data_completeness": "complete" if record.completed_at else "partial",
            "audit_status": "not_run",
            "events": [evt.to_dict() for evt in events],
            "results": [],
            "artifacts": [],
            "assessment_refs": [],
            "evidence_revision": 1,
            "record_revision": 0,
        }

        if record.output:
            catalog_record["results"] = record.output.get("results", [])
            catalog_record["operational_metrics"] = record.output.get(
                "operational_metrics", {}
            )

        return catalog_record