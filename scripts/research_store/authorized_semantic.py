"""Route structured semantic work through the run's persisted authority mode."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import model_gateway
from model_gateway import StructuredResult

from .execution_policy import ExecutionModeError


class HostArtifactSupplier(Protocol):
    def supply(
        self,
        *,
        semantic_context: dict[str, Any],
        schema: Mapping[str, Any],
        **call_kwargs: Any,
    ) -> StructuredResult:
        """Provide a genuinely external host-authored semantic artifact."""
        ...


def call_authorized_structured(
    *,
    semantic_service: Any,
    semantic_context: dict[str, Any],
    deterministic_fixture: dict[str, Any],
    actor_identifier: str,
    host_artifact_supplier: HostArtifactSupplier | None = None,
    **call_kwargs: Any,
) -> StructuredResult:
    """Execute or ingest one structured decision under exact run authority.

    Non-autonomous suppliers are deliberately gated to the real release
    harness. Normal production callers cannot silently manufacture host or
    fixture authority.
    """
    run_id = UUID(str(semantic_context["run_id"]))
    with semantic_service.uow_factory() as uow:
        status = uow.runs.get_run_status(run_id=run_id)
    mode = status["execution_mode"]

    if mode == "autonomous_local":
        return model_gateway.call_structured(
            **call_kwargs,
            semantic_persistence=semantic_service,
            semantic_context=semantic_context,
        )

    if mode == "agent_led":
        if host_artifact_supplier is None:
            raise ExecutionModeError(
                "agent_led semantic decisions require an explicit HostArtifactSupplier"
            )
        generated = host_artifact_supplier.supply(
            semantic_context=semantic_context,
            **call_kwargs,
        )
        if generated.error:
            raise ExecutionModeError(
                f"agent_led host artifact supplier failed: {generated.error}"
            )
        if not generated.value:
            raise ExecutionModeError(
                "agent_led host artifact supplier returned no artifact"
            )
        supplied_context = {
            **semantic_context,
            "supplied_response_metadata": {
                "provenance": generated.provenance,
                "attempts": list(generated.attempts),
                "usage": generated.provenance.get("usage", {}),
            },
        }
        ingested = semantic_service.ingest_host_artifact(
            supplied_context,
            generated.value,
            call_kwargs["schema"],
            actor_identifier=actor_identifier,
        )
        return _as_structured(ingested, generated.attempts)

    if mode == "deterministic_debug":
        if os.environ.get("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES") != "1":
            raise ExecutionModeError(
                "deterministic_debug requires an explicit deterministic fixture"
            )
        supplied_context = {
            **semantic_context,
            "supplied_response_metadata": {"not_invoked": True},
        }
        ingested = semantic_service.ingest_deterministic_fixture(
            supplied_context,
            deterministic_fixture,
            call_kwargs["schema"],
            actor_identifier=actor_identifier,
        )
        return _as_structured(ingested, ())

    raise ExecutionModeError(f"unsupported execution mode: {mode}")


def _as_structured(
    result: Any, attempts: tuple[Any, ...] | list[Any]
) -> StructuredResult:
    call_id = result.provenance.get("semantic_call_id")
    artifact_id = result.provenance.get("semantic_artifact_id")
    return StructuredResult(
        result.value,
        result.provenance,
        attempts,
        result.error or "",
        semantic_call_id=UUID(call_id) if call_id else None,
        artifact_ids=(UUID(artifact_id),) if artifact_id else (),
    )


__all__ = ["call_authorized_structured"]
