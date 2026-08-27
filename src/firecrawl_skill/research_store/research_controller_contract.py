"""Versioned public machine contracts for the canonical research controller."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .run_service import RunStatus

DIRECTIVE_SCHEMA_VERSION = "workflow-directive-v2"
RESULT_SCHEMA_VERSION = "research-result-v2"
CONTROLLER_POLICY_SCHEMA_VERSION = "research-controller-policy-v2"

DISPOSITION_CONTINUE = "continue_automatic"
DISPOSITION_OPERATOR = "operator_action_required"
DISPOSITION_COMPLETED = "terminal_completed"
DISPOSITION_PARTIAL = "terminal_partial"
DISPOSITION_BLOCKED = "blocked"
DISPOSITION_FAILED = "failed"
DISPOSITION_CANCELLED = "cancelled"

TERMINAL_DISPOSITIONS = frozenset(
    {
        DISPOSITION_COMPLETED,
        DISPOSITION_PARTIAL,
        DISPOSITION_FAILED,
        DISPOSITION_CANCELLED,
    }
)

_MAX_DIAGNOSTICS = 12
_MAX_TEXT = 500


class ControllerError(RuntimeError):
    """Base class for deterministic controller failures."""


class ControllerBlockedError(ControllerError):
    """Persisted authority is insufficient to continue without guessing."""


class ControllerBoundError(ControllerError):
    """A hard controller progress/deadline bound was exceeded."""


@dataclass(frozen=True)
class ControllerConfig:
    """Hard controller-side bounds independent of model/provider output."""

    max_actions: int = 32
    max_repeated_state: int = 2
    max_retained_candidates: int = 200
    max_deadline_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if self.max_repeated_state < 1:
            raise ValueError("max_repeated_state must be positive")
        if not 1 <= self.max_retained_candidates <= 200:
            raise ValueError("max_retained_candidates must be between 1 and 200")
        if self.max_deadline_seconds <= 0:
            raise ValueError("max_deadline_seconds must be positive")


@dataclass(frozen=True)
class WorkflowDirective:
    """Machine continuation authority returned by ``fresearch``."""

    schema_version: str
    run_id: str
    lifecycle_state: str
    lifecycle_revision: int
    disposition: str
    action_kind: str | None = None
    action_id: str | None = None
    diagnostics: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    result_ready: bool = False
    handoff_ready: bool = False
    objective_satisfied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_revision": self.lifecycle_revision,
            "disposition": self.disposition,
            "action_kind": self.action_kind,
            "action_id": self.action_id,
            "diagnostics": list(self.diagnostics),
            "limitations": list(self.limitations),
            "result_ready": self.result_ready,
            "handoff_ready": self.handoff_ready,
            "objective_satisfied": self.objective_satisfied,
        }


@dataclass(frozen=True)
class ResearchResult:
    """Bounded authoritative result/status contract for one public run."""

    schema_version: str
    run_id: str
    objective: str
    lifecycle_state: str
    lifecycle_revision: int
    disposition: str
    terminal: bool
    outcome: str | None
    result_ready: bool
    handoff_ready: bool
    objective_satisfied: bool
    action_kind: str | None = None
    action_id: str | None = None
    diagnostics: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "objective": self.objective,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_revision": self.lifecycle_revision,
            "disposition": self.disposition,
            "terminal": self.terminal,
            "outcome": self.outcome,
            "result_ready": self.result_ready,
            "handoff_ready": self.handoff_ready,
            "objective_satisfied": self.objective_satisfied,
            "action_kind": self.action_kind,
            "action_id": self.action_id,
            "diagnostics": list(self.diagnostics),
            "limitations": list(self.limitations),
        }


@dataclass
class ProgressGuard:
    """Detect repeated persisted directives and enforce hard action/deadline bounds."""

    config: ControllerConfig
    started: float = field(default_factory=time.monotonic)
    actions: int = 0
    seen: dict[tuple[str, int], int] = field(default_factory=dict)
    deadline_seconds: float | None = None

    def tighten_deadline(self, seconds: float) -> None:
        if seconds <= 0:
            raise ControllerBoundError("authoritative wall-clock budget is exhausted")
        effective = min(seconds, self.config.max_deadline_seconds)
        if self.deadline_seconds is None:
            self.deadline_seconds = effective
        else:
            self.deadline_seconds = min(self.deadline_seconds, effective)

    def observe(self, status: RunStatus) -> None:
        deadline = self.deadline_seconds or self.config.max_deadline_seconds
        if time.monotonic() - self.started > deadline:
            raise ControllerBoundError("controller deadline exceeded")
        self.actions += 1
        if self.actions > self.config.max_actions:
            raise ControllerBoundError("controller action bound exceeded")
        signature = (status.state, status.lifecycle_revision)
        count = self.seen.get(signature, 0) + 1
        self.seen[signature] = count
        if count > self.config.max_repeated_state:
            raise ControllerBoundError(
                "controller repeated the same persisted state without progress"
            )


def bounded_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:_MAX_TEXT]


def bounded_messages(values: list[Any] | tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(
        text
        for text in (bounded_text(value) for value in values[:_MAX_DIAGNOSTICS])
        if text
    )


def validate_public_run_id(value: str) -> str:
    """Require the existing public ``fr_<32 hex>`` identity form."""
    if not isinstance(value, str) or not value.startswith("fr_"):
        raise ValueError("research run ID must use public fr_<uuid> form")
    raw = value[3:]
    if len(raw) != 32:
        raise ValueError("research run ID must use public fr_<uuid> form")
    try:
        UUID(hex=raw)
    except ValueError as exc:
        raise ValueError("research run ID must use public fr_<uuid> form") from exc
    return value


def terminal_disposition(state: str) -> str:
    try:
        return {
            "completed": DISPOSITION_COMPLETED,
            "partial": DISPOSITION_PARTIAL,
            "failed": DISPOSITION_FAILED,
            "cancelled": DISPOSITION_CANCELLED,
        }[state]
    except KeyError as exc:
        raise ValueError(f"state is not terminal: {state}") from exc


__all__ = [
    "CONTROLLER_POLICY_SCHEMA_VERSION",
    "DIRECTIVE_SCHEMA_VERSION",
    "DISPOSITION_BLOCKED",
    "DISPOSITION_CANCELLED",
    "DISPOSITION_COMPLETED",
    "DISPOSITION_CONTINUE",
    "DISPOSITION_FAILED",
    "DISPOSITION_OPERATOR",
    "DISPOSITION_PARTIAL",
    "RESULT_SCHEMA_VERSION",
    "ControllerBlockedError",
    "ControllerBoundError",
    "ControllerConfig",
    "ControllerError",
    "ProgressGuard",
    "ResearchResult",
    "WorkflowDirective",
    "bounded_messages",
    "bounded_text",
    "terminal_disposition",
    "validate_public_run_id",
]
