"""Invocation event service — authoritative PostgreSQL event management.

This module provides the service layer for persisting, querying, and
sanitizing invocation events in PostgreSQL.  It is the sole authority
for new invocation events; filesystem records are derived *after*
database commit.

PRD mapping: FR-001, Section 14

Event types (constrained by the ``research_event_type`` enum in
migration 0016):

* ``run_started`` — research run began
* ``run_finished`` — research run completed / failed / partial
* ``run_reopened`` — terminal run reopened
* ``invocation_started`` — invocation began
* ``invocation_finished`` — invocation completed
* ``invocation_event`` — generic invocation event
* ``pivot`` — search pivot to new query
* ``retry`` — retry of a failed operation
* ``decision`` — deterministic decision (budget, strategy)
* ``recovery`` — recovery from a transient failure
* ``annotation`` — human or agent annotation

Authority model:

* PostgreSQL ``research_events`` table is authoritative.
* Filesystem event logs (``events.jsonl``) are derived compatibility
  exports, written *after* database commit.
* Event ordering is stable via ``sequence_number`` column.
* Sanitization and secret redaction are applied before storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4


def utcnow() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------
# Event type constants (must match the enum in migration 0016)
# ------------------------------------------------------------------

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "run_finished",
        "run_reopened",
        "invocation_started",
        "invocation_finished",
        "invocation_event",
        "pivot",
        "retry",
        "decision",
        "recovery",
        "annotation",
    }
)

# Keys whose values must always be redacted regardless of content.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


@dataclass(frozen=True)
class InvocationEvent:
    """Immutable representation of a single invocation event.

    Attributes:
        id: Event UUID (primary key in PostgreSQL).
        run_id: Associated research run.
        invocation_id: Parent invocation (NOT NULL after migration 0016).
        event_type: Constrained event type from the enum.
        actor_type: Who triggered the event (system, agent, operator).
        actor_identifier: Optional identifier for the actor.
        payload: Sanitized JSON payload.
        sequence_number: Stable ordering key within the run.
        run_revision: Run lifecycle revision at event time.
        created_at: Database timestamp.
    """

    id: UUID
    run_id: UUID
    invocation_id: UUID
    event_type: str
    actor_type: str
    payload: dict[str, Any]
    sequence_number: int
    run_revision: int
    created_at: datetime
    actor_identifier: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "InvocationEvent":
        """Construct from a database row mapping."""
        return cls(
            id=value["id"],
            run_id=value["run_id"],
            invocation_id=value["invocation_id"],
            event_type=value["event_type"],
            actor_type=value["actor_type"],
            payload=value.get("payload", {}),
            sequence_number=value["sequence_number"],
            run_revision=value["run_revision"],
            created_at=value["created_at"],
            actor_identifier=value.get("actor_identifier"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary suitable for compatibility export."""
        return {
            "event_id": str(self.id),
            "event_type": self.event_type,
            "actor_type": self.actor_type,
            "actor_identifier": self.actor_identifier,
            "payload": self.payload,
            "sequence_number": self.sequence_number,
            "run_revision": self.run_revision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True)
class EventAppendResult:
    """Result of appending an event to PostgreSQL.

    Attributes:
        event_id: The UUID of the appended event.
        sequence_number: Stable ordering position.
        run_revision: Run lifecycle revision at append time.
        reused: True if an identical event was already present (idempotent).
    """

    event_id: UUID
    sequence_number: int
    run_revision: int
    reused: bool

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EventAppendResult":
        return cls(
            event_id=value["event_id"],
            sequence_number=value["sequence_number"],
            run_revision=value["run_revision"],
            reused=value.get("reused", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "sequence_number": self.sequence_number,
            "run_revision": self.run_revision,
            "reused": self.reused,
        }


def _sanitize(value: Any, key: str = "") -> Any:
    """Recursively sanitize event payload data.

    Rules:
    * Keys in ``SENSITIVE_KEYS`` are always redacted to ``"[REDACTED]"``.
    * URLs are canonicalized (trailing slash removed, query params
      sorted, tracking params stripped, sensitive params redacted).
    * Text containing bearer tokens or API key patterns is redacted.
    """
    if key.lower() in {k.lower() for k in SENSITIVE_KEYS}:
        return "[REDACTED]"

    if isinstance(value, dict):
        return {k: _sanitize(v, k) for k, v in value.items()}

    if isinstance(value, list):
        return [_sanitize(item) for item in value]

    if isinstance(value, str):
        import re as _re

        # Bearer token redaction
        value = _re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
            "Bearer [REDACTED]",
            value,
        )
        # API key / token pattern redaction
        value = _re.sub(
            r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            value,
        )
        return value

    return value


def _validate_event_type(event_type: str) -> None:
    """Raise ``InvalidEventType`` if ``event_type`` is not in the allowed set."""
    if event_type not in EVENT_TYPES:
        raise InvalidEventType(
            f"invalid event type {event_type!r}; "
            f"allowed: {sorted(EVENT_TYPES)}"
        )


def _validate_run_id(run_id: UUID) -> None:
    """Raise ``ValueError`` if ``run_id`` is invalid."""
    if not isinstance(run_id, UUID):
        raise ValueError("run_id must be a UUID")


def _validate_invocation_id(invocation_id: UUID) -> None:
    """Raise ``ValueError`` if ``invocation_id`` is invalid."""
    if not isinstance(invocation_id, UUID):
        raise ValueError("invocation_id must be a UUID")


class InvocationEventError(Exception):
    """Base exception for invocation event errors."""


class InvalidEventType(InvocationEventError):
    """Raised when an event type is not in the allowed set."""


class StaleEventRevision(InvocationEventError):
    """Raised when an event append conflicts with a newer run revision."""


class DuplicateEventKey(InvocationEventError):
    """Raised when an idempotency key was used for a different event."""


class EventService:
    """Service layer for authoritative invocation event management.

    All event mutations go through PostgreSQL.  Filesystem event logs
    are derived *after* database commit.

    Args:
        uow_factory: Callable that returns a ``PostgresUnitOfWork``.
    """

    def __init__(self, uow_factory: Callable) -> None:
        self.uow_factory = uow_factory

    def append(
        self,
        run_id: UUID,
        event_type: str,
        actor_type: str,
        idempotency_key: str,
        *,
        invocation_id: UUID | None = None,
        actor_identifier: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventAppendResult:
        """Append a single event to PostgreSQL.

        The event is idempotent: if an identical event with the same
        ``idempotency_key`` already exists, the existing event is
        returned with ``reused=True``.

        Args:
            run_id: Research run UUID.
            event_type: Constrained event type.
            actor_type: Who triggered the event.
            idempotency_key: Deduplication key.
            invocation_id: Parent invocation UUID (optional for
                run-level events like ``run_started``).
            actor_identifier: Optional actor identifier.
            payload: Sanitized event payload.

        Returns:
            ``EventAppendResult`` with event metadata.

        Raises:
            InvalidEventType: If ``event_type`` is not allowed.
            ValueError: If required arguments are empty or malformed.
            DuplicateEventKey: If the idempotency key was used for
                a different event.
        """
        _validate_run_id(run_id)
        _validate_event_type(event_type)
        if not actor_type.strip():
            raise ValueError("actor_type is required")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")

        sanitized_payload = _sanitize(payload or {})

        with self.uow_factory() as uow:
            # Validate that the run exists before attempting to append
            cur = uow.connection.cursor()
            cur.execute(
                "SELECT id FROM research_runs WHERE id = %s", (run_id,),
            )
            if cur.fetchone() is None:
                raise KeyError(f"run {run_id} not found")
            try:
                event_id = uow.runs.append_event(
                    run_id,
                    event_type,
                    actor_type,
                    idempotency_key,
                    invocation_id=invocation_id,
                    actor_identifier=actor_identifier,
                    payload=sanitized_payload,
                )
            except ValueError as exc:
                if "idempotency key was used for another event" in str(exc):
                    raise DuplicateEventKey(
                        f"idempotency key {idempotency_key!r} was used for a "
                        f"different event: {exc}"
                    ) from exc
                raise
            # Fetch the full result including sequence_number
            row = uow.runs.get_event_by_id(run_id, event_id)
            return EventAppendResult.from_mapping(row)

    def list_events(
        self,
        run_id: UUID,
        *,
        invocation_id: UUID | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InvocationEvent]:
        """List events for a run, ordered by sequence_number.

        Args:
            run_id: Research run UUID.
            invocation_id: Optional filter by invocation.
            event_type: Optional filter by event type.
            limit: Maximum events to return.
            offset: Pagination offset.

        Returns:
            List of ``InvocationEvent`` objects ordered by sequence.
        """
        with self.uow_factory() as uow:
            rows = uow.runs.list_events(
                run_id,
                invocation_id=invocation_id,
                event_type=event_type,
                limit=limit,
                offset=offset,
            )
            return [InvocationEvent.from_mapping(row) for row in rows]

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
        with self.uow_factory() as uow:
            row = uow.runs.get_event_by_id(run_id, event_id)
            if row is None:
                raise KeyError(f"event {event_id} not found for run {run_id}")
            return InvocationEvent.from_mapping(row)

    def get_next_sequence(self, run_id: UUID) -> int:
        """Return the next available sequence number for a run.

        This is used by callers who need to compute the sequence
        number themselves (e.g. for compatibility exports).
        """
        with self.uow_factory() as uow:
            return uow.runs.next_event_sequence(run_id)

    def list_events_by_type(
        self,
        run_id: UUID,
        event_type: str,
        *,
        limit: int = 100,
    ) -> list[InvocationEvent]:
        """List events of a specific type for a run.

        Args:
            run_id: Research run UUID.
            event_type: Constrained event type.
            limit: Maximum events to return.

        Returns:
            List of ``InvocationEvent`` objects of the given type.
        """
        _validate_event_type(event_type)
        return self.list_events(run_id, event_type=event_type, limit=limit)

    def append_batch(
        self,
        run_id: UUID,
        events: list[dict[str, Any]],
        *,
        actor_type: str = "system",
    ) -> list[EventAppendResult]:
        """Append multiple events in a single transaction.

        All events are appended atomically.  If any event fails
        (e.g. duplicate key conflict with different payload), the
        entire batch is rolled back.

        Args:
            run_id: Research run UUID.
            events: List of event dicts with keys:
                ``event_type``, ``invocation_id``, ``payload``.
            actor_type: Default actor type for all events.

        Returns:
            List of ``EventAppendResult`` for each event.

        Raises:
            InvalidEventType: If any event has an invalid type.
            ValueError: If any event is malformed.
        """
        _validate_run_id(run_id)
        if not events:
            return []

        # Validate all events before appending any
        for evt in events:
            _validate_event_type(evt.get("event_type", ""))
            if not evt.get("payload"):
                raise ValueError("each event must have a payload")

        # Check for duplicate idempotency keys within the batch
        seen_keys: dict[str, int] = {}
        for idx, evt in enumerate(events):
            key = evt.get("idempotency_key", f"batch:{run_id}:{idx}:{uuid4()}")
            if key in seen_keys:
                raise DuplicateEventKey(
                    f"duplicate idempotency key {key!r} in batch at positions "
                    f"{seen_keys[key]} and {idx}"
                )
            seen_keys[key] = idx

        with self.uow_factory() as uow:
            results = []
            for idx, evt in enumerate(events):
                idempotency_key = evt.get(
                    "idempotency_key",
                    f"batch:{run_id}:{idx}:{uuid4()}",
                )
                try:
                    event_id = uow.runs.append_event(
                        run_id,
                        evt["event_type"],
                        actor_type,
                        idempotency_key,
                        invocation_id=evt.get("invocation_id"),
                        actor_identifier=evt.get("actor_identifier"),
                        payload=_sanitize(evt.get("payload", {})),
                    )
                except ValueError as exc:
                    if "idempotency key was used for another event" in str(exc):
                        raise DuplicateEventKey(
                            f"idempotency key {idempotency_key!r} was used for a "
                            f"different event: {exc}"
                        ) from exc
                    raise
                row = uow.runs.get_event_by_id(run_id, event_id)
                results.append(EventAppendResult.from_mapping(row))
            return results