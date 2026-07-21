"""Coverage-led adaptive workflow service.

This module implements the append-only coverage event ledger and
immutable coverage snapshots required by FR-012 (coverage-led adaptive
control).  Current coverage is a reconstructable projection from
events, not a mutable table.

Key invariants:

* coverage_events is append-only (enforced by DDL triggers).
* coverage_revision is monotonically increasing per run.
* Idempotent event application: duplicate idempotency keys are
  silently deduplicated and return the existing event.
* Stale updates are rejected: an event proposing a revision that
  does not exceed the current coverage revision raises
  StaleCoverageRevisionError.
* Unknown coverage-item references are rejected: inserting an event
  for an item_id that does not exist in coverage_events raises
  UnknownCoverageItemError.
* Deterministic projection rebuilding iterates events in
  (coverage_revision, id) order.

This service is intentionally thin — it contains no semantic
assessment logic.  Semantic judgments flow through
SemanticCallService; this service only persists deterministic
observations and status transitions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from research_domain.models import (
    CoverageItem,
    CoverageLedger,
    CoverageItemType,
    CoverageStatus,
    FreshnessStatus,
    OverallCoverageStatus,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_sha256(value: Any) -> str:
    """Return a content hash for a JSON-serializable value."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CoverageError(ValueError):
    """A coverage operation violated a schema or policy invariant."""


class StaleCoverageRevisionError(CoverageError):
    """An event proposes a revision that does not exceed the current one."""


class UnknownCoverageItemError(CoverageError):
    """An event references a coverage item that does not exist."""


class DuplicateCoverageEventError(CoverageError):
    """An idempotency key already exists for this run."""


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageEvent:
    id: UUID
    run_id: UUID
    coverage_revision: int
    prior_coverage_revision: int
    event_type: str
    item_id: UUID | None
    item_type: str | None
    subject_id: str | None
    new_status: str | None
    previous_status: str | None
    new_freshness_status: str | None
    previous_freshness_status: str | None
    source_event_id: UUID | None
    source_invocation_id: UUID | None
    payload: dict[str, Any]
    idempotency_key: str
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CoverageEvent":
        return cls(
            id=value["id"],
            run_id=value["run_id"],
            coverage_revision=value["coverage_revision"],
            prior_coverage_revision=value["prior_coverage_revision"],
            event_type=value["event_type"],
            item_id=value.get("item_id"),
            item_type=value.get("item_type"),
            subject_id=value.get("subject_id"),
            new_status=value.get("new_status"),
            previous_status=value.get("previous_status"),
            new_freshness_status=value.get("new_freshness_status"),
            previous_freshness_status=value.get("previous_freshness_status"),
            source_event_id=value.get("source_event_id"),
            source_invocation_id=value.get("source_invocation_id"),
            payload=value.get("payload", {}),
            idempotency_key=value["idempotency_key"],
            created_at=value["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "coverage_revision": self.coverage_revision,
            "prior_coverage_revision": self.prior_coverage_revision,
            "event_type": self.event_type,
            "item_id": str(self.item_id) if self.item_id else None,
            "item_type": self.item_type,
            "subject_id": self.subject_id,
            "new_status": self.new_status,
            "previous_status": self.previous_status,
            "new_freshness_status": self.new_freshness_status,
            "previous_freshness_status": self.previous_freshness_status,
            "source_event_id": str(self.source_event_id)
            if self.source_event_id
            else None,
            "source_invocation_id": str(self.source_invocation_id)
            if self.source_invocation_id
            else None,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }


@dataclass(frozen=True)
class CoverageSnapshot:
    id: UUID
    run_id: UUID
    coverage_revision: int
    ledger: dict[str, Any]
    content_sha256: str
    triggering_event_id: UUID | None
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CoverageSnapshot":
        return cls(
            id=value["id"],
            run_id=value["run_id"],
            coverage_revision=value["coverage_revision"],
            ledger=value["ledger"],
            content_sha256=value["content_sha256"],
            triggering_event_id=value.get("triggering_event_id"),
            created_at=value["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "coverage_revision": self.coverage_revision,
            "ledger": self.ledger,
            "content_sha256": self.content_sha256,
            "triggering_event_id": (
                str(self.triggering_event_id) if self.triggering_event_id else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CoverageService:
    """Append-only coverage event ledger and projection builder.

    Public API:

    * ``create_items_from_spec`` — seed coverage items from a ResearchSpec.
    * ``apply_event`` — apply one coverage event (idempotent, stale-reject).
    * ``rebuild_projection`` — rebuild current coverage from events.
    * ``create_snapshot`` — materialize an immutable ledger snapshot.
    * ``current_projection`` — return the latest snapshot or rebuild.
    * ``list_events`` — query events by run, item, or type.
    * ``get_snapshot`` — retrieve a snapshot by revision.
    """

    def __init__(self, uow_factory: Callable) -> None:
        self.uow_factory = uow_factory

    # ------------------------------------------------------------------
    # Item creation
    # ------------------------------------------------------------------

    def create_items_from_spec(
        self,
        run_id: UUID,
        spec: Mapping[str, Any],
        *,
        execution_mode: str = "deterministic_debug",
        idempotency_key: str | None = None,
        source_event_id: UUID | None = None,
        source_invocation_id: UUID | None = None,
    ) -> list[CoverageItem]:
        """Seed coverage items from a validated ResearchSpec.

        This is the initial population step.  Each question, claim, and
        requirement becomes a coverage item with status ``unassessed``.
        """
        if not run_id:
            raise CoverageError("run_id is required")

        questions = spec.get("questions", [])
        claims = spec.get("claims_to_validate", [])
        freshness_reqs = spec.get("freshness_requirements", [])
        source_reqs = spec.get("required_source_classes", [])
        corroboration_reqs = spec.get("corroboration_requirements", [])
        contradiction_reqs = spec.get("contradiction_requirements", [])

        items: list[dict[str, Any]] = []

        for q in questions:
            items.append(
                {
                    "item_type": "question",
                    "subject_id": str(q["question_id"]),
                    "text": q.get("text", ""),
                }
            )

        for c in claims:
            items.append(
                {
                    "item_type": "claim",
                    "subject_id": str(c["claim_id"]),
                    "text": c.get("statement", ""),
                }
            )

        for fr in freshness_reqs:
            items.append(
                {
                    "item_type": "freshness_requirement",
                    "subject_id": str(fr["requirement_id"]),
                    "text": fr.get("description", ""),
                }
            )

        for sr in source_reqs:
            items.append(
                {
                    "item_type": "source_requirement",
                    "subject_id": str(sr["requirement_id"]),
                    "text": sr.get("source_class", ""),
                }
            )

        for cr in corroboration_reqs:
            items.append(
                {
                    "item_type": "corroboration_requirement",
                    "subject_id": str(cr["requirement_id"]),
                    "text": cr.get("description", ""),
                }
            )

        for cr in contradiction_reqs:
            items.append(
                {
                    "item_type": "contradiction_requirement",
                    "subject_id": str(cr["requirement_id"]),
                    "text": cr.get("description", ""),
                }
            )

        if not items:
            raise CoverageError(
                "ResearchSpec must contain at least one question, claim, or requirement"
            )

        with self.uow_factory() as uow:
            event_ids = uow.coverage.create_items(
                run_id,
                items,
                idempotency_key=idempotency_key or f"spec:items:{run_id}",
                source_event_id=source_event_id,
                source_invocation_id=source_invocation_id,
                execution_mode=execution_mode,
            )

        return [
            CoverageItem(
                coverage_item_id=UUID(str(eid)),
                item_type=_str_to_item_type(item["item_type"]),
                subject_id=item["subject_id"],
                status=CoverageStatus.UNASSESSED,
                candidate_ids=(),
                snapshot_ids=(),
                passage_ids=(),
                independent_source_count=0,
                required_independent_source_count=0,
                authority_classes_present=(),
                freshness_status=FreshnessStatus.NOT_APPLICABLE,
                remaining_gap=item.get("text", ""),
                confidence=0.0,
                mechanical_failure_ids=(),
            )
            for item, eid in zip(items, event_ids)
        ]

    # ------------------------------------------------------------------
    # Event application
    # ------------------------------------------------------------------

    def apply_event(
        self,
        run_id: UUID,
        event_type: str,
        *,
        item_id: UUID | None = None,
        item_type: str | None = None,
        subject_id: str | None = None,
        new_status: str | None = None,
        previous_status: str | None = None,
        new_freshness_status: str | None = None,
        previous_freshness_status: str | None = None,
        source_event_id: UUID | None = None,
        source_invocation_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CoverageEvent:
        """Apply one coverage event.

        Idempotent: if ``idempotency_key`` already exists for this run,
        the original event is returned without side effects.

        Stale-reject: the new coverage_revision must exceed the current
        coverage revision stored on ``research_runs``.
        """
        if not run_id:
            raise CoverageError("run_id is required")
        if not event_type:
            raise CoverageError("event_type is required")
        if not idempotency_key:
            idempotency_key = f"event:{uuid4()}"
        if not idempotency_key.strip():
            raise CoverageError("idempotency_key must be nonempty")

        with self.uow_factory() as uow:
            result = uow.coverage.apply_event(
                run_id=run_id,
                event_type=event_type,
                item_id=item_id,
                item_type=item_type,
                subject_id=subject_id,
                new_status=new_status,
                previous_status=previous_status,
                new_freshness_status=new_freshness_status,
                previous_freshness_status=previous_freshness_status,
                source_event_id=source_event_id,
                source_invocation_id=source_invocation_id,
                payload=payload or {},
                idempotency_key=idempotency_key,
            )

        return CoverageEvent.from_mapping(result)

    # ------------------------------------------------------------------
    # Projection rebuilding
    # ------------------------------------------------------------------

    def rebuild_projection(
        self,
        run_id: UUID,
        *,
        idempotency_key: str | None = None,
        source_event_id: UUID | None = None,
    ) -> CoverageLedger:
        """Rebuild the current coverage projection from events.

        Deterministic: events are processed in
        ``(coverage_revision, id)`` order.  The resulting ledger is
        materialized as a snapshot.
        """
        with self.uow_factory() as uow:
            ledger = uow.coverage.rebuild_projection(
                run_id,
                idempotency_key=idempotency_key or f"rebuild:{run_id}",
                source_event_id=source_event_id,
            )
        return CoverageLedger(
            schema_version="coverage-ledger-v1",
            run_id=run_id,
            revision=ledger["revision"],
            items=tuple(
                CoverageItem(
                    coverage_item_id=UUID(str(item["coverage_item_id"])),
                    item_type=_str_to_item_type(item["item_type"]),
                    subject_id=item["subject_id"],
                    status=_str_to_coverage_status(item["status"]),
                    candidate_ids=tuple(
                        UUID(str(cid)) for cid in item.get("candidate_ids", [])
                    ),
                    snapshot_ids=tuple(
                        UUID(str(sid)) for sid in item.get("snapshot_ids", [])
                    ),
                    passage_ids=tuple(
                        UUID(str(pid)) for pid in item.get("passage_ids", [])
                    ),
                    independent_source_count=item.get("independent_source_count", 0),
                    required_independent_source_count=item.get(
                        "required_independent_source_count", 0
                    ),
                    authority_classes_present=tuple(
                        item.get("authority_classes_present", [])
                    ),
                    freshness_status=_str_to_freshness_status(
                        item.get("freshness_status", "not_applicable")
                    ),
                    remaining_gap=item.get("remaining_gap", ""),
                    confidence=item.get("confidence", 0.0),
                    mechanical_failure_ids=tuple(),
                )
                for item in ledger["items"]
            ),
            overall_status=_str_to_overall_status(
                ledger.get("overall_status", "unassessed")
            ),
            mechanical_failures=tuple(),
        )

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def create_snapshot(
        self,
        run_id: UUID,
        ledger: dict[str, Any],
        *,
        coverage_revision: int,
        idempotency_key: str | None = None,
        triggering_event_id: UUID | None = None,
    ) -> CoverageSnapshot:
        """Materialize an immutable ledger snapshot."""
        if not run_id:
            raise CoverageError("run_id is required")
        if coverage_revision < 1:
            raise CoverageError("coverage_revision must be positive")
        if not ledger:
            raise CoverageError("ledger content is required")

        content_hash = _json_sha256(ledger)
        with self.uow_factory() as uow:
            result = uow.coverage.create_snapshot(
                run_id=run_id,
                coverage_revision=coverage_revision,
                ledger=ledger,
                content_sha256=content_hash,
                idempotency_key=idempotency_key
                or f"snapshot:{run_id}:{coverage_revision}",
                triggering_event_id=triggering_event_id,
            )
        return CoverageSnapshot.from_mapping(result)

    def get_snapshot(
        self, run_id: UUID, coverage_revision: int
    ) -> CoverageSnapshot | None:
        with self.uow_factory() as uow:
            result = uow.coverage.get_snapshot(run_id, coverage_revision)
        if result is None:
            return None
        return CoverageSnapshot.from_mapping(result)

    def current_snapshot(self, run_id: UUID) -> CoverageSnapshot | None:
        with self.uow_factory() as uow:
            result = uow.coverage.get_latest_snapshot(run_id)
        if result is None:
            return None
        if isinstance(result, CoverageSnapshot):
            return result
        return CoverageSnapshot.from_mapping(result)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def list_events(
        self,
        run_id: UUID,
        *,
        item_id: UUID | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CoverageEvent]:
        with self.uow_factory() as uow:
            rows = uow.coverage.list_events(
                run_id,
                item_id=item_id,
                event_type=event_type,
                limit=limit,
                offset=offset,
            )
        return [CoverageEvent.from_mapping(r) for r in rows]

    def get_event(self, run_id: UUID, event_id: UUID) -> CoverageEvent:
        with self.uow_factory() as uow:
            result = uow.coverage.get_event(run_id, event_id)
        if result is None:
            raise UnknownCoverageItemError(
                f"coverage event {event_id} not found for run {run_id}"
            )
        return CoverageEvent.from_mapping(result)

    def get_current_revision(self, run_id: UUID) -> int:
        with self.uow_factory() as uow:
            return uow.coverage.get_current_revision(run_id)

    def count_events(self, run_id: UUID) -> int:
        with self.uow_factory() as uow:
            return uow.coverage.count_events(run_id)


# ---------------------------------------------------------------------------
# String <-> enum helpers
# ---------------------------------------------------------------------------


def _str_to_item_type(value: str) -> CoverageItemType:
    try:
        return CoverageItemType(value)
    except ValueError:
        raise CoverageError(f"unknown coverage item type: {value}")


def _str_to_coverage_status(value: str) -> CoverageStatus:
    try:
        return CoverageStatus(value)
    except ValueError:
        raise CoverageError(f"unknown coverage status: {value}")


def _str_to_freshness_status(value: str) -> FreshnessStatus:
    try:
        return FreshnessStatus(value)
    except ValueError:
        raise CoverageError(f"unknown freshness status: {value}")


def _str_to_overall_status(value: str) -> OverallCoverageStatus:
    try:
        return OverallCoverageStatus(value)
    except ValueError:
        raise CoverageError(f"unknown overall coverage status: {value}")


__all__ = [
    "CoverageService",
    "CoverageEvent",
    "CoverageSnapshot",
    "CoverageError",
    "StaleCoverageRevisionError",
    "UnknownCoverageItemError",
    "DuplicateCoverageEventError",
]
