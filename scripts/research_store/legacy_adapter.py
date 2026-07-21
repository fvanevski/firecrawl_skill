"""Thin legacy entry-point adapter and shadow-comparison policy.

This module deliberately does not implement planning, acquisition, extraction,
coverage, or synthesis logic. It only maps legacy entry-point decisions onto
existing service boundaries and records the comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Callable


ENTRY_POINT_OPERATIONS = {
    "frun": "research_run.lifecycle",
    "fsearch_smart": "research_workflow.orchestrate",
    "fsearch": "acquisition.single_query",
    "fscrape": "extraction.batch",
}


class LegacyAdapterError(RuntimeError):
    """A compatibility adapter request is invalid or could not be persisted."""


class AdapterMode(str, Enum):
    COMPATIBILITY = "compatibility"
    SHADOW = "shadow"
    AUTHORITATIVE = "authoritative"

    @classmethod
    def parse(cls, value: str) -> "AdapterMode":
        try:
            return cls(value)
        except ValueError as exc:
            raise LegacyAdapterError(
                "FIRECRAWL_LEGACY_ADAPTER_MODE must be compatibility, shadow, "
                "or authoritative"
            ) from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    """Strip volatile values before behavior comparison."""
    if isinstance(value, dict):
        return {
            key: _normalize(item)
            for key, item in sorted(value.items())
            if key not in {"generated_at", "created_at", "duration_ms", "scratch_dir"}
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


@dataclass(frozen=True)
class AdapterResult:
    mode: str
    entry_point: str
    service_operation: str
    recorded: bool
    authoritative_write: bool
    comparison_id: Any | None = None
    invocation_id: Any | None = None
    divergent: bool | None = None
    divergence_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "entry_point": self.entry_point,
            "service_operation": self.service_operation,
            "recorded": self.recorded,
            "authoritative_write": self.authoritative_write,
            "comparison_id": self.comparison_id,
            "invocation_id": self.invocation_id,
            "divergent": self.divergent,
            "divergence_reasons": list(self.divergence_reasons),
        }


class LegacyEntryPointAdapter:
    """Route one completed legacy decision through existing workflow services."""

    def __init__(self, uow_factory: Callable | None, mode: AdapterMode):
        self.uow_factory = uow_factory
        self.mode = mode

    def route(
        self,
        entry_point: str,
        legacy_decision: dict[str, Any],
        *,
        external_run_id: str | None = None,
        external_invocation_id: str | None = None,
        service_proposal: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> AdapterResult:
        if entry_point not in ENTRY_POINT_OPERATIONS:
            raise LegacyAdapterError(f"unsupported legacy entry point: {entry_point}")
        if not idempotency_key.strip():
            raise LegacyAdapterError("adapter idempotency key is required")
        service_operation = ENTRY_POINT_OPERATIONS[entry_point]
        proposal = {
            "entry_point": entry_point,
            "operation": service_operation,
            "action": legacy_decision.get("action"),
            "status": legacy_decision.get("status"),
            "input": legacy_decision.get("input", {}),
        }
        if service_proposal is not None:
            proposal.update(service_proposal)
        if self.mode == AdapterMode.COMPATIBILITY:
            return AdapterResult(
                mode=self.mode.value,
                entry_point=entry_point,
                service_operation=service_operation,
                recorded=False,
                authoritative_write=False,
            )
        if self.uow_factory is None:
            raise LegacyAdapterError(
                f"{self.mode.value} adapter mode requires the authoritative database"
            )
        legacy_normalized = _normalize(legacy_decision)
        proposal_normalized = _normalize(proposal)
        reasons = []
        if legacy_normalized.get("action") != proposal_normalized.get("action"):
            reasons.append("action")
        if legacy_normalized.get("status") != proposal_normalized.get("status"):
            reasons.append("status")
        legacy_input = legacy_normalized.get("input", {})
        if legacy_input != proposal_normalized.get("input", {}):
            reasons.append("input")
        divergent = bool(reasons)
        try:
            with self.uow_factory() as uow:
                run = None
                if external_run_id:
                    try:
                        run = uow.runs.get_run_status(external_id=external_run_id)
                    except KeyError:
                        if self.mode == AdapterMode.AUTHORITATIVE:
                            raise LegacyAdapterError(
                                "authoritative adapter mode requires an existing research run"
                            )
                resolved_key = idempotency_key.replace(
                    "{workflow_revision}",
                    str(run["lifecycle_revision"]) if run else "unbound",
                )
                invocation_id = None
                if self.mode == AdapterMode.AUTHORITATIVE and entry_point != "frun":
                    if run is None:
                        raise LegacyAdapterError(
                            "authoritative adapter mode requires --research-run-id for "
                            f"{entry_point}"
                        )
                    invocation_id = uow.runs.record_invocation(
                        run["id"],
                        service_operation,
                        f"{resolved_key}:invocation",
                        external_invocation_id=external_invocation_id,
                        status=legacy_decision.get("status", "complete"),
                        input_payload=legacy_decision.get("input", {}),
                        metadata={"legacy_entry_point": entry_point},
                    )
                    uow.runs.append_event(
                        run["id"],
                        "legacy_adapter.routed",
                        "compatibility-adapter",
                        f"{resolved_key}:event",
                        invocation_id=invocation_id,
                        payload={
                            "entry_point": entry_point,
                            "service_operation": service_operation,
                            "legacy_status": legacy_decision.get("status"),
                        },
                    )
                comparison_id = uow.runs.record_legacy_adapter_comparison(
                    entry_point,
                    self.mode.value,
                    legacy_normalized,
                    proposal_normalized,
                    _digest(legacy_normalized),
                    _digest(proposal_normalized),
                    divergent,
                    reasons,
                    resolved_key,
                    run_id=run["id"] if run else None,
                    external_run_id=external_run_id,
                    external_invocation_id=external_invocation_id,
                    workflow_revision=run["lifecycle_revision"] if run else None,
                )
        except LegacyAdapterError:
            raise
        except Exception as exc:
            raise LegacyAdapterError(f"legacy adapter persistence failed: {exc}") from exc
        return AdapterResult(
            mode=self.mode.value,
            entry_point=entry_point,
            service_operation=service_operation,
            recorded=True,
            authoritative_write=self.mode == AdapterMode.AUTHORITATIVE,
            comparison_id=comparison_id,
            invocation_id=invocation_id,
            divergent=divergent,
            divergence_reasons=tuple(reasons),
        )


__all__ = [
    "AdapterMode",
    "AdapterResult",
    "ENTRY_POINT_OPERATIONS",
    "LegacyAdapterError",
    "LegacyEntryPointAdapter",
]
