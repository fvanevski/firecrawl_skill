from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from uuid import UUID


RUN_STATES = frozenset(
    {
        "created",
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
        "coverage_review",
        "retrieving",
        "synthesizing",
        "validating",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
)
TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})
PERMITTED_TRANSITIONS = {
    "created": frozenset({"planning"}),
    "planning": frozenset({"corpus_review", "failed"}),
    "corpus_review": frozenset({"acquiring", "retrieving", "failed"}),
    "acquiring": frozenset({"coverage_review", "extracting", "failed"}),
    "extracting": frozenset({"indexing", "coverage_review", "failed"}),
    "indexing": frozenset({"coverage_review", "partial", "failed"}),
    "coverage_review": frozenset(
        {"acquiring", "extracting", "retrieving", "synthesizing", "partial", "failed"}
    ),
    "retrieving": frozenset({"coverage_review", "synthesizing", "failed"}),
    "synthesizing": frozenset({"validating", "failed"}),
    "validating": frozenset({"completed", "partial", "failed"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class RunStateError(ValueError):
    """A requested run mutation violates lifecycle policy."""


class StaleRunRevisionError(RunStateError):
    """A command was proposed against an older lifecycle revision."""


@dataclass(frozen=True)
class RunStatus:
    id: UUID
    external_id: str | None
    state: str
    lifecycle_revision: int
    reopened_from_revision: int | None
    execution_mode: str
    objective: str
    declared_outcome: str | None
    legacy_status: str
    completed_at: datetime | None
    error: str | None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "RunStatus":
        return cls(
            id=value["id"],
            external_id=value.get("external_id"),
            state=value["state"],
            lifecycle_revision=value["lifecycle_revision"],
            reopened_from_revision=value.get("reopened_from_revision"),
            execution_mode=value["execution_mode"],
            objective=value["objective"],
            declared_outcome=value.get("declared_outcome"),
            legacy_status=value["legacy_status"],
            completed_at=value.get("completed_at"),
            error=value.get("error"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "state": self.state,
            "lifecycle_revision": self.lifecycle_revision,
            "reopened_from_revision": self.reopened_from_revision,
            "execution_mode": self.execution_mode,
            "objective": self.objective,
            "declared_outcome": self.declared_outcome,
            "legacy_status": self.legacy_status,
            "completed_at": self.completed_at,
            "error": self.error,
            "terminal": self.state in TERMINAL_STATES,
        }


@dataclass(frozen=True)
class TransitionResult:
    transition_id: UUID
    event_id: UUID
    prior_state: str
    next_state: str
    lifecycle_revision: int
    reused: bool

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "TransitionResult":
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "event_id": self.event_id,
            "prior_state": self.prior_state,
            "next_state": self.next_state,
            "lifecycle_revision": self.lifecycle_revision,
            "reused": self.reused,
        }


def is_transition_permitted(prior_state: str, next_state: str) -> bool:
    return next_state in PERMITTED_TRANSITIONS.get(prior_state, ())


class ResearchRunService:
    """Authoritative run lifecycle policy over a transactional repository."""

    def __init__(self, uow_factory: Callable, policy_version: str = "run-state-v1"):
        self.uow_factory = uow_factory
        self.policy_version = policy_version

    def create(
        self,
        objective: str,
        external_id: str,
        *,
        execution_mode: str = "agent_led",
        idempotency_key: str | None = None,
        actor_type: str = "system",
        actor_identifier: str | None = None,
        metadata: dict[str, Any] | None = None,
        **legacy_metadata: Any,
    ) -> RunStatus:
        if not objective.strip():
            raise ValueError("research objective is required")
        if not external_id.strip():
            raise ValueError("external run ID is required")
        command_key = idempotency_key or f"run:create:{external_id}"
        run_metadata = dict(legacy_metadata)
        run_metadata.update(
            {
                "external_run_id": external_id,
                "execution_mode": execution_mode,
                "metadata": metadata or {},
            }
        )
        with self.uow_factory() as uow:
            run_id = uow.runs.start_run(objective, run_metadata)
            uow.runs.append_event(
                run_id,
                "run.created",
                actor_type,
                command_key,
                actor_identifier=actor_identifier,
                payload={
                    "objective": objective,
                    "execution_mode": execution_mode,
                    "policy_version": self.policy_version,
                },
            )
            return RunStatus.from_mapping(uow.runs.get_run_status(run_id=run_id))

    def status(
        self, *, run_id: UUID | None = None, external_id: str | None = None
    ) -> RunStatus:
        with self.uow_factory() as uow:
            return RunStatus.from_mapping(
                uow.runs.get_run_status(run_id=run_id, external_id=external_id)
            )

    def transition(
        self,
        run_id: UUID,
        next_state: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_identifier: str | None = None,
        semantic_proposal_id: UUID | None = None,
        triggering_event: str | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
        completion: dict[str, Any] | None = None,
    ) -> TransitionResult:
        if next_state not in RUN_STATES:
            raise RunStateError(f"unknown research run state: {next_state}")
        permitted_prior_states = frozenset(
            state
            for state, destinations in PERMITTED_TRANSITIONS.items()
            if next_state in destinations
        )
        if not permitted_prior_states:
            raise RunStateError(
                f"state {next_state!r} is reachable only through an explicit lifecycle command"
            )
        return self._apply(
            run_id,
            next_state,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            semantic_proposal_id=semantic_proposal_id,
            triggering_event=triggering_event or f"run.transitioned.{next_state}",
            reason=reason,
            outcome=outcome,
            error=error,
            completion=completion,
            permitted_prior_states=permitted_prior_states,
        )

    def complete(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self.transition(run_id, "completed", **command)

    def partial(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self.transition(run_id, "partial", **command)

    def fail(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self.transition(run_id, "failed", **command)

    def cancel(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_identifier: str | None = None,
        reason: str | None = None,
    ) -> TransitionResult:
        return self._apply(
            run_id,
            "cancelled",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            triggering_event="run.cancelled",
            reason=reason,
            outcome="cancelled",
            error=reason,
            permitted_prior_states=RUN_STATES - TERMINAL_STATES,
        )

    def reopen(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_identifier: str | None = None,
        reason: str,
    ) -> TransitionResult:
        if not reason.strip():
            raise ValueError("reopen reason is required")
        return self._apply(
            run_id,
            "created",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            triggering_event="run.reopened",
            reason=reason,
            permitted_prior_states=TERMINAL_STATES,
            reopen=True,
        )

    def _apply(
        self,
        run_id: UUID,
        next_state: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        permitted_prior_states: frozenset[str],
        actor_identifier: str | None = None,
        semantic_proposal_id: UUID | None = None,
        triggering_event: str,
        reason: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
        completion: dict[str, Any] | None = None,
        reopen: bool = False,
    ) -> TransitionResult:
        if expected_revision < 0:
            raise ValueError("expected revision must be non-negative")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        with self.uow_factory() as uow:
            try:
                result = uow.runs.apply_run_transition(
                    run_id,
                    next_state,
                    expected_revision,
                    idempotency_key,
                    actor_type,
                    self.policy_version,
                    permitted_prior_states=permitted_prior_states,
                    actor_identifier=actor_identifier,
                    semantic_proposal_id=semantic_proposal_id,
                    event_type=triggering_event,
                    reason=reason,
                    outcome=outcome,
                    error=error,
                    completion=completion or {},
                    reopen=reopen,
                )
            except ValueError as exc:
                if str(exc).startswith("stale research run revision"):
                    raise StaleRunRevisionError(str(exc)) from exc
                if str(exc).startswith("research run transition rejected"):
                    raise RunStateError(str(exc)) from exc
                raise
        return TransitionResult.from_mapping(result)
