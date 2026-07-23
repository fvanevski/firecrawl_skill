from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from .execution_policy import ExecutionModePolicy


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
    "acquiring": frozenset({"coverage_review", "extracting", "failed", "partial"}),
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


@dataclass(frozen=True)
class ModeChangeResult:
    event_id: UUID
    prior_mode: str
    next_mode: str
    lifecycle_revision: int
    reused: bool

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ModeChangeResult":
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "prior_mode": self.prior_mode,
            "next_mode": self.next_mode,
            "lifecycle_revision": self.lifecycle_revision,
            "reused": self.reused,
        }


def is_transition_permitted(prior_state: str, next_state: str) -> bool:
    return next_state in PERMITTED_TRANSITIONS.get(prior_state, ())


class ResearchRunService:
    """Authoritative run lifecycle policy over a transactional repository."""

    def __init__(
        self,
        uow_factory: Callable,
        policy_version: str = "run-state-v1",
        blob_store: Any | None = None,
    ):
        self.uow_factory = uow_factory
        self.policy_version = policy_version
        self.blob_store = blob_store
        self.execution_policy = ExecutionModePolicy()
        # Lazily initialized event service to avoid circular imports
        self._event_service = None

    @property
    def event_service(self):
        """Lazily initialized EventService to avoid circular imports.

        The EventService is created on first access and cached in ``_event_service``.
        The ``uow_factory`` is captured at creation time and never changes.
        """
        if self._event_service is None:
            from .invocation_events import EventService

            self._event_service = EventService(self.uow_factory)
        return self._event_service

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
        self.execution_policy.validate_mode(execution_mode)
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
        self.event_service.append(
            run_id,
            "run_started",
            actor_type,
            command_key,
            actor_identifier=actor_identifier,
            payload={
                "objective": objective,
                "execution_mode": execution_mode,
                "policy_version": self.policy_version,
            },
        )
        with self.uow_factory() as uow:
            return RunStatus.from_mapping(uow.runs.get_run_status(run_id=run_id))

    def change_execution_mode(
        self,
        run_id: UUID,
        next_mode: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        requested_by: str,
        approved_by: str,
        reason: str,
        actor_type: str = "operator",
        actor_identifier: str | None = None,
    ) -> ModeChangeResult:
        self.execution_policy.validate_mode(next_mode)
        if expected_revision < 0:
            raise ValueError("expected revision must be non-negative")
        for label, value in (
            ("idempotency key", idempotency_key),
            ("mode-change requester", requested_by),
            ("mode-change approver", approved_by),
            ("mode-change reason", reason),
        ):
            if not value.strip():
                raise ValueError(f"{label} is required")
        with self.uow_factory() as uow:
            try:
                result = uow.runs.revise_execution_mode(
                    run_id,
                    next_mode,
                    expected_revision,
                    idempotency_key,
                    actor_type,
                    self.execution_policy.version,
                    requested_by=requested_by,
                    approved_by=approved_by,
                    reason=reason,
                    actor_identifier=actor_identifier,
                )
            except ValueError as exc:
                if str(exc).startswith("stale research run revision"):
                    raise StaleRunRevisionError(str(exc)) from exc
                if str(exc).startswith("research run mode change rejected"):
                    raise RunStateError(str(exc)) from exc
                raise
        return ModeChangeResult.from_mapping(result)

    def status(
        self, *, run_id: UUID | None = None, external_id: str | None = None
    ) -> RunStatus:
        with self.uow_factory() as uow:
            return RunStatus.from_mapping(
                uow.runs.get_run_status(run_id=run_id, external_id=external_id)
            )

    def run_exists(self, run_id: UUID) -> bool:
        """Return True if a research run with the given ID exists."""
        try:
            self.status(run_id=run_id)
            return True
        except KeyError:
            return False

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

    def record_research_spec(
        self,
        run_id: UUID,
        spec: dict[str, Any] | Any,
        revision: int = 1,
        idempotency_key: str | None = None,
        **metadata: Any,
    ) -> UUID:
        with self.uow_factory() as uow:
            res = uow.runs.record_research_spec(
                run_id,
                spec_revision=revision,
                schema_name="research_spec",
                schema_version=1,
                payload=spec,
                idempotency_key=idempotency_key or f"spec_raw:{run_id}:{revision}",
                **metadata,
            )
            return res

    def record_search_plan(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        revision: int,
        search_plan: dict[str, Any] | Any,
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID:
        with self.uow_factory() as uow:
            return uow.runs.record_search_plan(
                run_id,
                research_spec_id,
                revision,
                search_plan,
                idempotency_key,
                **metadata,
            )

    def get_search_plan(
        self, run_id: UUID, plan_id: UUID | None = None, revision: int | None = None
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.get_search_plan(run_id, plan_id=plan_id, revision=revision)

    def list_search_plans(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.runs.list_search_plans(run_id)

    def get_plan_query(
        self, query_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.get_plan_query(query_id, run_id=run_id)

    def list_plan_queries(self, plan_id: UUID) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.runs.list_plan_queries(plan_id)

    def record_search_response(
        self,
        run_id: UUID,
        query_text: str,
        backend: str,
        raw_payload: bytes | str,
        idempotency_key: str,
        blob_store: Any | None = None,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        provider_request_id: str | None = None,
        parser_version: str = "firecrawl-search-v1",
        http_status: int | None = None,
        error_message: str | None = None,
        requested_at: Any | None = None,
        responded_at: Any | None = None,
        transport_metadata: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        store = blob_store or self.blob_store
        if store is None:
            import os
            from pathlib import Path
            from .blob import ContentAddressedBlobStore

            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )
        with self.uow_factory() as uow:
            return uow.runs.record_search_response(
                run_id,
                query_text,
                backend,
                raw_payload,
                idempotency_key,
                store,
                plan_id=plan_id,
                plan_query_id=plan_query_id,
                provider_request_id=provider_request_id,
                parser_version=parser_version,
                http_status=http_status,
                error_message=error_message,
                requested_at=requested_at,
                responded_at=responded_at,
                transport_metadata=transport_metadata,
                **metadata,
            )

    def get_search_response(
        self, response_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.get_search_response(response_id, run_id=run_id)

    def list_search_responses(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.runs.list_search_responses(
                run_id, plan_id=plan_id, plan_query_id=plan_query_id, status=status
            )

    def replay_search_response(
        self,
        response_id: UUID,
        run_id: UUID | None = None,
        blob_store: Any | None = None,
    ) -> Any:
        from .replay import SearchResponseReplayReader

        store = blob_store or self.blob_store
        if store is None:
            import os
            from pathlib import Path
            from .blob import ContentAddressedBlobStore

            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )
        with self.uow_factory() as uow:
            reader = SearchResponseReplayReader(uow.runs, store)
            return reader.replay_search_response(response_id, run_id=run_id)

    def record_response_candidates(
        self,
        run_id: UUID,
        search_response_id: UUID,
        blob_store: Any | None = None,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        store = blob_store or self.blob_store
        if store is None:
            import os
            from pathlib import Path
            from .blob import ContentAddressedBlobStore

            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )
        with self.uow_factory() as uow:
            return uow.runs.record_response_candidates(
                run_id,
                search_response_id,
                store,
                plan_id=plan_id,
                plan_query_id=plan_query_id,
            )

    def get_candidate(
        self, candidate_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.get_candidate(candidate_id, run_id=run_id)

    def list_candidates(
        self,
        run_id: UUID,
        *,
        domain: str | None = None,
        min_recurrence: int | None = None,
        duplicate_group_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.runs.list_candidates(
                run_id,
                domain=domain,
                min_recurrence=min_recurrence,
                duplicate_group_id=duplicate_group_id,
            )

    def list_candidate_occurrences(
        self, candidate_id: UUID, run_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.runs.list_candidate_occurrences(candidate_id, run_id=run_id)

    def assign_duplicate_group(
        self,
        candidate_ids: list[UUID],
        group_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> UUID:
        with self.uow_factory() as uow:
            return uow.runs.assign_duplicate_group(
                candidate_ids, group_id=group_id, run_id=run_id
            )

    def list_candidates_paginated(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        query_text: str | None = None,
        domain: str | None = None,
        min_recurrence: int | None = None,
        duplicate_group_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.list_candidates_paginated(
                run_id,
                plan_id=plan_id,
                plan_query_id=plan_query_id,
                query_text=query_text,
                domain=domain,
                min_recurrence=min_recurrence,
                duplicate_group_id=duplicate_group_id,
                limit=limit,
                offset=offset,
            )

    def get_candidate_card(
        self,
        candidate_id: UUID,
        run_id: UUID | None = None,
        *,
        max_snippet_length: int = 500,
        max_occurrences: int = 10,
    ) -> dict[str, Any]:
        with self.uow_factory() as uow:
            cand = uow.runs.get_candidate(candidate_id, run_id=run_id)
            occs = uow.runs.list_candidate_occurrences(candidate_id, run_id=run_id)

            snippet = cand.get("snippet")
            if snippet and len(snippet) > max_snippet_length:
                snippet = snippet[:max_snippet_length].rstrip() + "..."

            pub_date = cand.get("published_at")
            pub_date_str = (
                pub_date.isoformat()
                if hasattr(pub_date, "isoformat")
                else (str(pub_date) if pub_date else None)
            )

            occ_summaries = []
            for occ in occs[:max_occurrences]:
                disc_at = occ.get("discovered_at")
                disc_at_str = (
                    disc_at.isoformat()
                    if hasattr(disc_at, "isoformat")
                    else (str(disc_at) if disc_at else None)
                )
                occ_summaries.append(
                    {
                        "query_text": occ.get("query_text"),
                        "rank": occ.get("rank"),
                        "plan_id": str(occ["plan_id"]) if occ.get("plan_id") else None,
                        "plan_query_id": str(occ["plan_query_id"])
                        if occ.get("plan_query_id")
                        else None,
                        "discovered_at": disc_at_str,
                    }
                )

            return {
                "id": str(cand["id"]),
                "run_id": str(cand["run_id"]),
                "canonical_url": cand["canonical_url"],
                "original_url": cand["original_url"],
                "domain": cand["domain"],
                "title": cand.get("title"),
                "snippet": snippet,
                "published_at": pub_date_str,
                "recurrence_count": cand["recurrence_count"],
                "duplicate_group_id": str(cand["duplicate_group_id"])
                if cand.get("duplicate_group_id")
                else None,
                "date_signals": cand.get("date_signals", {}),
                "backend_metadata": cand.get("backend_metadata", {}),
                "occurrences": occ_summaries,
            }

    def build_triage_input(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        query_text: str | None = None,
        domain: str | None = None,
        min_recurrence: int | None = None,
        duplicate_group_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        max_snippet_length: int = 500,
    ) -> dict[str, Any]:
        paginated = self.list_candidates_paginated(
            run_id,
            plan_id=plan_id,
            plan_query_id=plan_query_id,
            query_text=query_text,
            domain=domain,
            min_recurrence=min_recurrence,
            duplicate_group_id=duplicate_group_id,
            limit=limit,
            offset=offset,
        )

        cards = [
            self.get_candidate_card(
                UUID(str(item["id"])),
                run_id=run_id,
                max_snippet_length=max_snippet_length,
            )
            for item in paginated["items"]
        ]

        from .domain import utcnow

        return {
            "run_id": str(run_id),
            "candidate_cards": cards,
            "total_count": paginated["total_count"],
            "limit": paginated["limit"],
            "offset": paginated["offset"],
            "has_next": paginated["has_next"],
            "filters_applied": {
                "plan_id": str(plan_id) if plan_id else None,
                "plan_query_id": str(plan_query_id) if plan_query_id else None,
                "query_text": query_text,
                "domain": domain,
                "min_recurrence": min_recurrence,
                "duplicate_group_id": str(duplicate_group_id)
                if duplicate_group_id
                else None,
            },
            "generated_at": utcnow().isoformat(),
        }

    def replay_candidates(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        domain: str | None = None,
        min_recurrence: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Replay candidate corpus for a run offline without live acquisition."""
        return self.build_triage_input(
            run_id,
            plan_id=plan_id,
            plan_query_id=plan_query_id,
            domain=domain,
            min_recurrence=min_recurrence,
            limit=limit,
            offset=offset,
        )

    def annotate(
        self,
        run_id: UUID,
        event_type: str,
        reason: str,
        *,
        from_invocation: str | None = None,
        to_invocation: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        actor_type: str = "cli",
    ) -> dict[str, Any]:
        """Append an annotation event to a research run.

        This is a compatibility command routed through PostgreSQL (issue #36).
        Annotations are stored as events in ``research_events``.

        Args:
            run_id: Research run UUID.
            event_type: Annotation type (pivot, retry, decision).
            reason: Human-readable reason.
            from_invocation: Optional source invocation ID.
            to_invocation: Optional target invocation ID.
            expected_revision: Compare-and-swap revision.
            idempotency_key: Deduplication key.
            actor_type: Actor type.

        Returns:
            Dict with event_id, run_id, lifecycle_revision, prior_revision.
        """
        if not reason.strip():
            raise ValueError("annotate reason is required")
        if expected_revision is None:
            with self.uow_factory() as uow:
                status_data = uow.runs.get_run_status(run_id=run_id)
                expected_revision = status_data["lifecycle_revision"]
        if expected_revision < 0:
            raise ValueError("expected revision must be non-negative")
        key = idempotency_key or f"run:annotate:{run_id}:{event_type}:{reason}"
        payload: dict[str, Any] = {
            "event_type": event_type,
            "reason": reason,
        }
        if from_invocation:
            payload["from_invocation"] = from_invocation
        if to_invocation:
            payload["to_invocation"] = to_invocation
        with self.uow_factory() as uow:
            # Read current state for compare-and-swap
            status_data = uow.runs.get_run_status(run_id=run_id)
            if status_data["lifecycle_revision"] != expected_revision:
                raise RunStateError(
                    f"stale research run revision: expected {expected_revision}, "
                    f"got {status_data['lifecycle_revision']}"
                )
            event_id = uow.runs.append_event(
                run_id,
                "annotation",
                actor_type,
                key,
                payload=payload,
            )
            # Bump lifecycle revision
            new_revision = expected_revision + 1
            uow.connection.execute(
                "UPDATE research_runs SET lifecycle_revision=%s WHERE id=%s",
                (new_revision, run_id),
            )
        return {
            "event_id": str(event_id),
            "run_id": str(run_id),
            "lifecycle_revision": new_revision,
            "prior_revision": expected_revision,
            "event_type": event_type,
        }

    def verify(self, run_id: UUID) -> dict[str, Any]:
        """Verify blob integrity for a research run.

        Checks all snapshot blobs referenced by invocations in the run.

        Args:
            run_id: Research run UUID.

        Returns:
            Verification report with available, missing, hash_mismatch counts.
        """
        with self.uow_factory() as uow:
            # Get all invocations for this run
            invocations = uow.runs.list_invocations(run_id)
            total = 0
            available = 0
            missing = 0
            hash_mismatch = 0
            artifacts = []

            for inv in invocations:
                output = inv.get("output") or {}
                for result in output.get("results", []):
                    for artifact_key in ("snapshot", "artifacts"):
                        artifact = result.get(artifact_key)
                        if isinstance(artifact, dict):
                            path = artifact.get("path")
                            expected_hash = artifact.get("sha256")
                            if path and expected_hash:
                                total += 1
                                if self.blob_store and self.blob_store.verify(
                                    expected_hash
                                ):
                                    available += 1
                                    artifacts.append(
                                        {
                                            "invocation_id": str(inv["id"]),
                                            "path": path,
                                            "state": "available",
                                        }
                                    )
                                else:
                                    hash_mismatch += 1
                                    artifacts.append(
                                        {
                                            "invocation_id": str(inv["id"]),
                                            "path": path,
                                            "state": "hash_mismatch",
                                        }
                                    )
                        elif isinstance(artifact, list):
                            for item in artifact:
                                path = item.get("path")
                                expected_hash = item.get("sha256")
                                if path and expected_hash:
                                    total += 1
                                    if self.blob_store and self.blob_store.verify(
                                        expected_hash
                                    ):
                                        available += 1
                                        artifacts.append(
                                            {
                                                "invocation_id": str(inv["id"]),
                                                "path": path,
                                                "state": "available",
                                            }
                                        )
                                    else:
                                        hash_mismatch += 1
                                        artifacts.append(
                                            {
                                                "invocation_id": str(inv["id"]),
                                                "path": path,
                                                "state": "hash_mismatch",
                                            }
                                        )

            return {
                "target": str(run_id),
                "verified_at": datetime.now(timezone).isoformat(),
                "total": total,
                "available": available,
                "missing": missing,
                "hash_mismatch": hash_mismatch,
                "artifacts": artifacts,
            }

    def trigger_audit(
        self,
        run_id: UUID,
        *,
        target_hash: str,
        provider: str = "local",
        model: str | None = None,
        force: bool = False,
        stages: list[str] | None = None,
        max_calls: int | None = None,
        max_input_tokens: int | None = None,
        fallback_provider: str | None = None,
        fallback_model: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a semantic audit for a research run.

        This delegates to the audit service. The audit is stored in
        PostgreSQL and the result is returned.

        Args:
            run_id: Research run UUID.
            target_hash: SHA-256 hash of the audit packet.
            provider: LLM provider (local, openai, gemini).
            model: Model name.
            force: Force re-audit even if current assessment exists.
            stages: Stages to run (rubric, acquisition, evidence, synthesis).
            max_calls: Maximum LLM calls.
            max_input_tokens: Target input tokens per chunk.
            fallback_provider: Commercial fallback provider.
            fallback_model: Fallback model name.

        Returns:
            Audit result dict with assessment_id, status, stages.
        """
        from .container import build_audit_service
        from .config import StoreConfig
        from .postgres import PostgresUnitOfWork

        config = StoreConfig.from_env()
        audit_service = build_audit_service(
            lambda: PostgresUnitOfWork(
                config.database_url,
                config.physical_collection,
                config.embedding_model,
                config.embedding_revision,
                config.embedding_dimension,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            )
        )
        stage_set = stages or ["rubric", "acquisition", "evidence", "synthesis"]
        result = audit_service.schedule_assessment(
            run_id,
            target_type="run",
            target_id=run_id,
            target_hash=target_hash,
            evaluator_version="catalog-v5.0",
            prompt_template_version="staged-research-audit-v1",
            policy_version="audit-policy-v1",
            stage_set=stage_set,
            status="partial",
            provider=provider,
            model=model,
        )
        return result
