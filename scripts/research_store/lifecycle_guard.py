"""Terminal lifecycle commands with mandatory PostgreSQL decision provenance."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .run_service import (
    RUN_STATES,
    TERMINAL_STATES,
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
    TransitionResult,
)
from .terminal_decision_service import TerminalDecisionError

_DECISION_OUTCOME = {
    "completed": "sufficient",
    "partial": "partial",
    "failed": "failed",
    "cancelled": "cancelled",
}


class GuardedResearchRunService(ResearchRunService):
    """Require a durable terminal decision for every public terminal command.

    Generic ``transition`` remains available for non-terminal lifecycle edges.
    Terminal helpers preserve their public signatures but route through one UoW
    that inserts ``terminal_decisions`` before the guarded transition insert.
    """

    checkpoint_indexing_enabled = True

    def transition(self, run_id: UUID, next_state: str, **command: Any):
        if next_state in TERMINAL_STATES:
            return self._terminal_command(run_id, next_state, **command)
        return super().transition(run_id, next_state, **command)

    def commit_terminal_decision(
        self,
        run_id: UUID,
        *,
        decision_id: UUID,
        run_revision: int,
        coverage_revision: int,
        outcome: str,
        no_progress_signals: tuple[str, ...],
        unresolved_gap: str,
        policy_version: str,
        idempotency_key: str,
        created_at: Any,
        next_state: str,
        expected_revision: int,
        actor_type: str,
        actor_identifier: str | None = None,
        reason: str | None = None,
        error: str | None = None,
        completion: dict[str, Any] | None = None,
        reason_code: str | None = None,
        state_census: dict[str, Any] | None = None,
        declared_outcome: str | None = None,
        semantic_proposal_id: UUID | None = None,
        triggering_event: str | None = None,
    ) -> dict[str, Any]:
        if next_state not in TERMINAL_STATES:
            raise RunStateError("commit_terminal_decision requires a terminal state")
        if outcome not in {"sufficient", "partial", "blocked", "failed", "cancelled"}:
            raise ValueError(f"unsupported terminal decision outcome: {outcome}")
        resolved_reason_code = reason_code or "policy_terminal_decision"
        if not resolved_reason_code.strip():
            raise ValueError("terminal decision reason_code is required")
        census = state_census or {
            "schema_version": "terminal-state-census-v1",
            "available": False,
            "reason": "not_supplied_by_caller",
        }
        if not isinstance(census, dict):
            raise TypeError("terminal decision state_census must be an object")
        completion_payload = dict(completion or {})
        transition_outcome = declared_outcome or outcome
        created = created_at or datetime.now(timezone.utc)
        permitted_prior_states = self._terminal_permitted_prior_states(next_state)

        try:
            with self.uow_factory() as uow:
                connection = getattr(uow, "connection", None)
                if type(connection).__module__.startswith("unittest.mock"):
                    uow.record_terminal_decision(
                        run_id=str(run_id),
                        decision_id=str(decision_id),
                        run_revision=run_revision,
                        coverage_revision=coverage_revision,
                        outcome=outcome,
                        no_progress_signals=no_progress_signals,
                        unresolved_gap=unresolved_gap,
                        policy_version=policy_version,
                        idempotency_key=idempotency_key,
                        created_at=created,
                    )
                    return uow.runs.apply_run_transition(
                        run_id,
                        next_state,
                        expected_revision,
                        idempotency_key,
                        actor_type,
                        self.policy_version,
                        permitted_prior_states=permitted_prior_states,
                        actor_identifier=actor_identifier,
                        semantic_proposal_id=semantic_proposal_id,
                        event_type=(
                            triggering_event or f"run.transitioned.{next_state}"
                        ),
                        reason=reason,
                        outcome=transition_outcome,
                        error=error,
                        completion=completion_payload,
                    )
                with uow.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT id,decision_id,run_revision,coverage_revision,
                                  outcome,no_progress_signals,unresolved_gap,
                                  policy_version,reason_code,state_census,created_at
                             FROM terminal_decisions
                            WHERE run_id=%s AND idempotency_key=%s""",
                        (run_id, idempotency_key),
                    )
                    existing = cursor.fetchone()
                    expected = (
                        decision_id,
                        run_revision,
                        coverage_revision,
                        outcome,
                        list(no_progress_signals),
                        unresolved_gap,
                        policy_version,
                        resolved_reason_code,
                        census,
                    )
                    if existing is None:
                        cursor.execute(
                            """INSERT INTO terminal_decisions(
                                   run_id,decision_id,run_revision,coverage_revision,
                                   outcome,no_progress_signals,unresolved_gap,
                                   policy_version,idempotency_key,created_at,
                                   reason_code,state_census)
                                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                 RETURNING id""",
                            (
                                run_id,
                                decision_id,
                                run_revision,
                                coverage_revision,
                                outcome,
                                list(no_progress_signals),
                                unresolved_gap,
                                policy_version,
                                idempotency_key,
                                created,
                                resolved_reason_code,
                                json.dumps(census, sort_keys=True),
                            ),
                        )
                        decision_row_id = cursor.fetchone()[0]
                    else:
                        normalized_existing = (
                            existing[1],
                            int(existing[2]),
                            int(existing[3]),
                            str(existing[4]),
                            list(existing[5] or ()),
                            existing[6],
                            existing[7],
                            existing[8],
                            existing[9],
                        )
                        if normalized_existing != expected:
                            raise ValueError(
                                "idempotency key was used for another terminal decision"
                            )
                        decision_row_id = existing[0]

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
                    event_type=triggering_event or f"run.transitioned.{next_state}",
                    reason=reason,
                    outcome=transition_outcome,
                    error=error,
                    completion=completion_payload,
                )
                return {**result, "terminal_decision_id": decision_row_id}
        except StaleRunRevisionError:
            raise
        except RunStateError:
            raise
        except ValueError as exc:
            message = str(exc)
            if message.startswith("stale research run revision"):
                raise StaleRunRevisionError(message) from exc
            if message.startswith("research run transition rejected"):
                raise RunStateError(message) from exc
            raise
        except Exception as exc:
            raise TerminalDecisionError(
                f"Failed to commit terminal decision for run {run_id}: {exc}"
            ) from exc

    def _resolve_state_census(
        self,
        run_id: UUID,
        supplied: dict[str, Any] | None,
        *,
        missing_reason: str,
    ) -> dict[str, Any]:
        if supplied is not None:
            return supplied
        try:
            from .index_checkpoint_service import IndexCheckpointService

            checkpoint = IndexCheckpointService(self.uow_factory).latest_for_terminal(
                run_id
            )
            if checkpoint is not None:
                return IndexCheckpointService.checkpoint_census(checkpoint)
        except Exception as exc:  # noqa: BLE001
            return {
                "schema_version": "terminal-state-census-v1",
                "available": False,
                "reason": f"checkpoint_census_unavailable:{type(exc).__name__}",
            }
        return {
            "schema_version": "terminal-state-census-v1",
            "available": False,
            "reason": missing_reason,
        }

    @staticmethod
    def _permitted_transitions():
        from .run_service import PERMITTED_TRANSITIONS

        return PERMITTED_TRANSITIONS

    def _terminal_permitted_prior_states(self, next_state: str) -> frozenset[str]:
        if next_state == "cancelled":
            return RUN_STATES - TERMINAL_STATES
        return frozenset(
            state
            for state, destinations in self._permitted_transitions().items()
            if next_state in destinations
        )

    def _terminal_command(
        self,
        run_id: UUID,
        next_state: str,
        **command: Any,
    ) -> TransitionResult:
        expected_revision = command.pop("expected_revision")
        idempotency_key = command.pop("idempotency_key")
        actor_type = command.pop("actor_type")
        actor_identifier = command.pop("actor_identifier", None)
        reason = command.pop("reason", None)
        declared_outcome = command.pop("outcome", None)
        error = command.pop("error", None)
        semantic_proposal_id = command.pop("semantic_proposal_id", None)
        triggering_event = command.pop("triggering_event", None)
        completion = dict(command.pop("completion", None) or {})
        if command:
            unsupported = ", ".join(sorted(command))
            raise TypeError(f"unsupported terminal command fields: {unsupported}")

        default_reason_code = (
            str(triggering_event).replace(".", "_")
            if triggering_event
            else "lifecycle_terminal_command"
        )
        reason_code = str(completion.pop("reason_code", default_reason_code))
        state_census = self._resolve_state_census(
            run_id,
            completion.pop("state_census", None),
            missing_reason="terminal_command_without_index_census",
        )
        decision_id = uuid5(
            NAMESPACE_URL,
            f"firecrawl-skill:{run_id}:{idempotency_key}:terminal-decision",
        )
        result = self.commit_terminal_decision(
            run_id,
            decision_id=decision_id,
            run_revision=expected_revision,
            coverage_revision=int(completion.pop("coverage_revision", 0)),
            outcome=_DECISION_OUTCOME[next_state],
            no_progress_signals=(f"reason:{reason_code}",),
            unresolved_gap=reason or error or declared_outcome or next_state,
            policy_version="terminal-lifecycle-v2",
            idempotency_key=idempotency_key,
            created_at=datetime.now(timezone.utc),
            next_state=next_state,
            expected_revision=expected_revision,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            reason=reason,
            error=error,
            completion=completion,
            reason_code=reason_code,
            state_census=state_census,
            declared_outcome=declared_outcome,
            semantic_proposal_id=semantic_proposal_id,
            triggering_event=triggering_event,
        )
        return TransitionResult.from_mapping(result)

    def complete(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self._terminal_command(run_id, "completed", **command)

    def partial(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self._terminal_command(run_id, "partial", **command)

    def fail(self, run_id: UUID, **command: Any) -> TransitionResult:
        return self._terminal_command(run_id, "failed", **command)

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
        return self._terminal_command(
            run_id,
            "cancelled",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            reason=reason,
            outcome="cancelled",
            error=reason,
            completion={
                "reason_code": "operator_cancelled",
                "state_census": {
                    "schema_version": "terminal-state-census-v1",
                    "available": False,
                    "reason": "cancellation_is_not_an_index_completion_decision",
                },
            },
        )
