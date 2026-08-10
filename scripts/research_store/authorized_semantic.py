"""Route structured semantic work through the run's persisted authority mode."""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol
from uuid import UUID

import model_gateway
from model_gateway import StructuredResult

from .execution_policy import ExecutionModeError
from .semantic_service import validate_structured_payload


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


def _citation_verdict_schema(
    full_schema: Mapping[str, Any], expected_count: int
) -> dict[str, Any]:
    """Return the semantic-only schema used by the autonomous citation model.

    Citation identity is immutable draft state, not a semantic decision. The
    local model therefore supplies only one ordered status/issue verdict per
    draft reference. Exact section/claim/passage identity is rebound
    deterministically after the model call.
    """
    item_schema = full_schema["properties"]["validation_results"]["items"]
    properties = item_schema["properties"]
    return {
        "type": "object",
        "required": ["validation_results"],
        "properties": {
            "validation_results": {
                "type": "array",
                "minItems": expected_count,
                "maxItems": expected_count,
                "items": {
                    "type": "object",
                    "required": ["status", "issue"],
                    "properties": {
                        "status": deepcopy(properties["status"]),
                        "issue": deepcopy(properties["issue"]),
                    },
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    }


def _bind_citation_verdicts(
    deterministic_fixture: Mapping[str, Any], verdict_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind ordered semantic verdicts to exact deterministic citation identity."""
    expected = list(deterministic_fixture.get("validation_results", ()))
    verdicts = verdict_payload.get("validation_results")
    if not isinstance(verdicts, list):
        raise ValueError("citation verdict output is missing validation_results")
    if len(verdicts) != len(expected):
        raise ValueError(
            "citation verdict count mismatch: "
            f"expected {len(expected)}, got {len(verdicts)}"
        )

    canonical = deepcopy(dict(deterministic_fixture))
    canonical["validation_results"] = [
        {
            "section_id": identity["section_id"],
            "claim_id": identity["claim_id"],
            "passage_ids": list(identity["passage_ids"]),
            "status": verdict["status"],
            "issue": verdict["issue"],
        }
        for identity, verdict in zip(expected, verdicts, strict=True)
    ]
    return canonical


class _CitationPersistence:
    """Persist canonical citation artifacts while the model emits verdicts only."""

    def __init__(
        self,
        delegate: Any,
        *,
        full_schema: Mapping[str, Any],
        deterministic_fixture: Mapping[str, Any],
    ) -> None:
        self.delegate = delegate
        self.full_schema = full_schema
        self.deterministic_fixture = deterministic_fixture

    def start_model_call(self, context: Mapping[str, Any], **kwargs: Any) -> UUID:
        # The recorded request schema remains the truthful schema presented to
        # the model (semantic verdicts only).
        return self.delegate.start_model_call(context, **kwargs)

    def finish_model_call(
        self,
        context: Mapping[str, Any],
        call_id: UUID,
        *,
        status: str,
        provenance: Mapping[str, Any],
        attempts: list[Mapping[str, Any]],
        artifacts: list[Mapping[str, Any]],
        error: str = "",
    ) -> tuple[UUID, ...]:
        transformed: list[dict[str, Any]] = []
        for artifact in artifacts:
            validation_errors = list(artifact.get("validation_errors", ()))
            try:
                payload = _bind_citation_verdicts(
                    self.deterministic_fixture,
                    artifact.get("payload") or {},
                )
                validation_errors.extend(
                    validate_structured_payload(payload, self.full_schema)
                )
            except (KeyError, TypeError, ValueError) as exc:
                payload = {
                    "model_verdict": deepcopy(artifact.get("payload")),
                }
                validation_errors.append(
                    f"deterministic citation identity binding failed: {exc}"
                )
            transformed.append(
                {
                    **dict(artifact),
                    "payload": payload,
                    "validation_errors": validation_errors,
                }
            )

        persisted_status = status
        persisted_error = error
        if status == "complete" and (
            not transformed or transformed[-1]["validation_errors"]
        ):
            persisted_status = "failed"
            persisted_error = persisted_error or (
                "canonical citation artifact failed deterministic identity binding"
            )

        return self.delegate.finish_model_call(
            context,
            call_id,
            status=persisted_status,
            provenance={
                **dict(provenance),
                "citation_identity_binding": "deterministic",
            },
            attempts=attempts,
            artifacts=transformed,
            error=persisted_error,
        )


def _call_autonomous_citation(
    *,
    semantic_service: Any,
    semantic_context: dict[str, Any],
    deterministic_fixture: dict[str, Any],
    call_kwargs: dict[str, Any],
) -> StructuredResult:
    """Run citation semantics without delegating immutable IDs to the model."""
    full_schema = call_kwargs["schema"]
    expected_count = len(deterministic_fixture.get("validation_results", ()))
    verdict_schema = _citation_verdict_schema(full_schema, expected_count)
    original_post_validate = call_kwargs.get("post_validate")

    def _validate_verdicts(payload: dict[str, Any]) -> None:
        verdicts = payload.get("validation_results")
        if not isinstance(verdicts, list) or len(verdicts) != expected_count:
            raise ValueError(
                "citation verdict count mismatch: "
                f"expected {expected_count}, got "
                f"{len(verdicts) if isinstance(verdicts, list) else 'non-array'}"
            )
        if original_post_validate is not None:
            original_post_validate(payload)

    kwargs = dict(call_kwargs)
    kwargs["schema"] = verdict_schema
    kwargs["post_validate"] = _validate_verdicts
    kwargs["system_prompt"] = (
        str(call_kwargs["system_prompt"])
        + " Immutable citation identity is bound deterministically outside the model."
        + " Return only validation_results, with exactly one status/issue verdict"
        + " for each draft claim reference in traversal order. Do not reproduce"
        + " section_id, claim_id, or passage_ids."
    )
    kwargs["user_prompt"] = (
        str(call_kwargs["user_prompt"])
        + f"\n\nReturn exactly {expected_count} ordered citation verdict(s)."
    )

    persistence = _CitationPersistence(
        semantic_service,
        full_schema=full_schema,
        deterministic_fixture=deterministic_fixture,
    )
    result = model_gateway.call_structured(
        **kwargs,
        semantic_persistence=persistence,
        semantic_context=semantic_context,
    )
    if result.error:
        return result

    try:
        canonical = _bind_citation_verdicts(
            deterministic_fixture,
            result.value or {},
        )
        validation_errors = validate_structured_payload(canonical, full_schema)
        if validation_errors:
            raise ValueError("; ".join(validation_errors[:10]))
    except (KeyError, TypeError, ValueError) as exc:
        return StructuredResult(
            None,
            {
                **dict(result.provenance),
                "citation_identity_binding": "deterministic",
            },
            result.attempts,
            f"canonical citation artifact validation failed: {exc}",
            semantic_call_id=result.semantic_call_id,
            artifact_ids=result.artifact_ids,
        )

    return StructuredResult(
        canonical,
        {
            **dict(result.provenance),
            "citation_identity_binding": "deterministic",
        },
        result.attempts,
        semantic_call_id=result.semantic_call_id,
        artifact_ids=result.artifact_ids,
    )


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
        if semantic_context.get("stage") == "citation_pass":
            return _call_autonomous_citation(
                semantic_service=semantic_service,
                semantic_context=semantic_context,
                deterministic_fixture=deterministic_fixture,
                call_kwargs=dict(call_kwargs),
            )
        if semantic_context.get("stage") == "claim_binding":
            # Claim binding is compact but occasionally reaches the local
            # completion limit with malformed/truncated JSON. The gateway now
            # expands length-truncated JSON retries; opt this stage into that
            # recovery rather than failing all attempts at the initial budget.
            call_kwargs = {
                **call_kwargs,
                "expand_output_on_length": True,
            }
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
