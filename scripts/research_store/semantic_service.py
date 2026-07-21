"""Durable provenance for structured semantic proposals."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping
from uuid import UUID

from .execution_policy import ExecutionModePolicy, SemanticAuthority


_SENSITIVE_KEY = re.compile(
    r"^(?:authorization|proxy[-_]authorization|cookie|set[-_]cookie|password|passwd|secret|client[-_]secret|api[-_]?key|apikey|access[-_]token|refresh[-_]token|auth[-_]token|token)$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|key)=)[^&#\s]+"
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|password|secret|key)\s*[:=]\s*)[^\s,;&]+"
)


def redact_sensitive(value: Any) -> Any:
    """Return a JSON-compatible value with credential material removed."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        text = _BEARER.sub(r"\1[REDACTED]", value)
        text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
        return _ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive(str(value))


def _redact_schema(value: Any) -> Any:
    """Preserve schema property names while sanitizing embedded examples/defaults."""
    if isinstance(value, Mapping):
        return {str(key): _redact_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_schema(item) for item in value]
    return redact_sensitive(value)


def validate_structured_payload(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate the JSON-Schema subset used by current semantic contracts."""
    errors: list[str] = []
    expected = schema.get("type")
    types = tuple(expected) if isinstance(expected, list) else (expected,) if expected else ()
    matches = (
        ("object" in types and isinstance(value, dict))
        or ("array" in types and isinstance(value, list))
        or ("string" in types and isinstance(value, str))
        or ("boolean" in types and isinstance(value, bool))
        or ("integer" in types and isinstance(value, int) and not isinstance(value, bool))
        or ("number" in types and isinstance(value, (int, float)) and not isinstance(value, bool))
        or ("null" in types and value is None)
    )
    if types and not matches:
        return [f"{path}: expected {'|'.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field {key}")
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path}: unexpected field {key}")
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_structured_payload(item, properties[key], f"{path}.{key}"))
    if isinstance(value, list) and schema.get("items"):
        for index, item in enumerate(value):
            errors.extend(validate_structured_payload(item, schema["items"], f"{path}[{index}]"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    return errors


@dataclass(frozen=True)
class HostArtifactResult:
    value: dict[str, Any] | None
    provenance: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    error: str = ""


class SemanticCallService:
    """Persist model calls and host-agent proposals through one validation path."""

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory
        self.execution_policy = ExecutionModePolicy()

    def _authorize(
        self, context: Mapping[str, Any], authority: SemanticAuthority
    ) -> tuple[UUID, dict[str, Any]]:
        run_id = UUID(str(context.get("run_id")))
        if context.get("run_revision") is None:
            raise ValueError("semantic call context is missing: run_revision")
        with self.uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
        expected_revision = int(context["run_revision"])
        if status["lifecycle_revision"] != expected_revision:
            raise ValueError(
                "stale semantic decision revision: "
                f"expected {expected_revision}, current {status['lifecycle_revision']}"
            )
        self.execution_policy.authorize(status["execution_mode"], authority)
        return run_id, status

    @staticmethod
    def _required_context(context: Mapping[str, Any]) -> tuple[UUID, str, str, int, str]:
        required = ("run_id", "stage", "schema_name", "schema_version", "idempotency_key")
        missing = [name for name in required if context.get(name) in (None, "")]
        if missing:
            raise ValueError(f"semantic call context is missing: {', '.join(missing)}")
        schema_version = int(context["schema_version"])
        if schema_version < 1:
            raise ValueError("semantic schema version must be positive")
        return (
            UUID(str(context["run_id"])),
            str(context["stage"]),
            str(context["schema_name"]),
            schema_version,
            str(context["idempotency_key"]),
        )

    def start_model_call(
        self,
        context: Mapping[str, Any],
        *,
        provider: str,
        requested_model: str,
        model_revision: str,
        endpoint_alias: str | None,
        prompt_version: str,
        prompt_hash: str,
        schema: Mapping[str, Any],
        input_token_estimate: int,
    ) -> UUID:
        run_id, stage, schema_name, schema_version, idempotency_key = self._required_context(context)
        _run_id, status = self._authorize(context, SemanticAuthority.LOCAL_MODEL)
        request = redact_sensitive(
            {
                "authority": "model",
                "endpoint_alias": endpoint_alias,
                "prompt_hash": prompt_hash,
                "schema_name": schema_name,
                "schema_version": schema_version,
                "input_artifact_ids": [str(item) for item in context.get("input_artifact_ids", ())],
                "input_token_estimate": input_token_estimate,
                "policy_version": context.get("policy_version"),
                "fallback_from_call_id": context.get("fallback_from_call_id"),
            }
        )
        request["schema"] = _redact_schema(schema)
        with self.uow_factory() as uow:
            return uow.runs.record_semantic_call(
                run_id,
                stage,
                provider,
                requested_model,
                prompt_version,
                request,
                idempotency_key,
                invocation_id=context.get("invocation_id"),
                model_revision=model_revision,
                status="running",
                expected_revision=int(context["run_revision"]),
                expected_execution_mode=status["execution_mode"],
            )

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
        run_id, _stage, schema_name, schema_version, idempotency_key = self._required_context(context)
        response_metadata = redact_sensitive(
            {
                "provenance": provenance,
                "attempts": attempts,
                "attempt_count": len(attempts),
                "fallback_from_call_id": context.get("fallback_from_call_id"),
            }
        )
        artifact_ids = []
        with self.uow_factory() as uow:
            uow.runs.finalize_semantic_call(
                run_id,
                call_id,
                status,
                response_metadata,
                redact_sensitive(error) if error else None,
            )
            for artifact in artifacts:
                attempt_number = int(artifact["attempt"])
                artifact_ids.append(
                    uow.runs.record_semantic_artifact(
                        run_id,
                        call_id,
                        str(context.get("artifact_type") or schema_name),
                        schema_name,
                        schema_version,
                        redact_sensitive(artifact["payload"]),
                        f"{idempotency_key}:artifact:{attempt_number}",
                        validation_status="valid" if not artifact.get("validation_errors") else "invalid",
                        validation_errors=redact_sensitive(artifact.get("validation_errors", [])),
                    )
                )
        return tuple(artifact_ids)

    def mark_fallback(
        self, run_id: UUID, call_id: UUID, *, provider: str, model: str
    ) -> None:
        with self.uow_factory() as uow:
            uow.runs.annotate_semantic_call(
                run_id,
                call_id,
                {"fallback": {"used": True, "provider": provider, "requested_model": model}},
            )

    def ingest_host_artifact(
        self,
        context: Mapping[str, Any],
        payload: dict[str, Any],
        schema: Mapping[str, Any],
        *,
        actor_identifier: str | None = None,
    ) -> HostArtifactResult:
        self._authorize(context, SemanticAuthority.HOST_AGENT)
        return self._ingest_supplied_artifact(
            context,
            payload,
            schema,
            authority=SemanticAuthority.HOST_AGENT,
            actor_identifier=actor_identifier,
        )

    def ingest_deterministic_fixture(
        self,
        context: Mapping[str, Any],
        payload: dict[str, Any],
        schema: Mapping[str, Any],
        *,
        actor_identifier: str | None = None,
    ) -> HostArtifactResult:
        self._authorize(context, SemanticAuthority.DETERMINISTIC_FIXTURE)
        return self._ingest_supplied_artifact(
            context,
            payload,
            schema,
            authority=SemanticAuthority.DETERMINISTIC_FIXTURE,
            actor_identifier=actor_identifier,
        )

    def _ingest_supplied_artifact(
        self,
        context: Mapping[str, Any],
        payload: dict[str, Any],
        schema: Mapping[str, Any],
        *,
        authority: SemanticAuthority,
        actor_identifier: str | None,
    ) -> HostArtifactResult:
        run_id, stage, schema_name, schema_version, idempotency_key = self._required_context(context)
        sanitized_payload = redact_sensitive(payload)
        validation_errors = validate_structured_payload(sanitized_payload, schema)
        request = redact_sensitive(
            {
                "authority": authority.value,
                "schema_name": schema_name,
                "schema_version": schema_version,
                "input_artifact_ids": [str(item) for item in context.get("input_artifact_ids", ())],
                "actor_identifier": actor_identifier,
                "policy_version": context.get("policy_version"),
            }
        )
        request["schema"] = _redact_schema(schema)
        with self.uow_factory() as uow:
            call_id = uow.runs.record_semantic_call(
                run_id,
                stage,
                authority.value,
                "",
                str(context.get("prompt_version") or f"{authority.value}-supplied"),
                request,
                idempotency_key,
                invocation_id=context.get("invocation_id"),
                status="running",
                expected_revision=int(context["run_revision"]),
                expected_execution_mode={
                    SemanticAuthority.HOST_AGENT: "agent_led",
                    SemanticAuthority.DETERMINISTIC_FIXTURE: "deterministic_debug",
                }[authority],
            )
            artifact_id = uow.runs.record_semantic_artifact(
                run_id,
                call_id,
                str(context.get("artifact_type") or schema_name),
                schema_name,
                schema_version,
                sanitized_payload,
                f"{idempotency_key}:artifact:1",
                validation_status="invalid" if validation_errors else "valid",
                validation_errors=redact_sensitive(validation_errors),
            )
            uow.runs.finalize_semantic_call(
                run_id,
                call_id,
                "failed" if validation_errors else "complete",
                {
                    "authority": authority.value,
                    "actor_identifier": actor_identifier,
                    "validation_errors": redact_sensitive(validation_errors),
                    "transport_attempts": [],
                    "semantic_coverage": (
                        "unassessed"
                        if authority == SemanticAuthority.DETERMINISTIC_FIXTURE
                        else "assessed"
                    ),
                },
                "; ".join(validation_errors) if validation_errors else None,
            )
        provenance = {
            "semantic_call_id": str(call_id),
            "semantic_artifact_id": str(artifact_id),
            "authority": authority.value,
            "schema_name": schema_name,
            "schema_version": schema_version,
        }
        return HostArtifactResult(
            None if validation_errors else sanitized_payload,
            provenance,
            (),
            "; ".join(validation_errors),
        )

    def decide(
        self,
        context: Mapping[str, Any],
        schema: Mapping[str, Any],
        *,
        host_artifact: dict[str, Any] | None = None,
        deterministic_fixture: dict[str, Any] | None = None,
        local_decision: Callable[..., Any] | None = None,
        actor_identifier: str | None = None,
    ) -> Any:
        """Route one decision without allowing silent semantic-authority fallback."""
        run_id = UUID(str(context.get("run_id")))
        with self.uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
        authority = self.execution_policy.route(
            status["execution_mode"],
            host_artifact_supplied=host_artifact is not None,
            deterministic_fixture_supplied=deterministic_fixture is not None,
        )
        if authority == SemanticAuthority.HOST_AGENT:
            return self.ingest_host_artifact(
                context, host_artifact, schema, actor_identifier=actor_identifier
            )
        if authority == SemanticAuthority.DETERMINISTIC_FIXTURE:
            return self.ingest_deterministic_fixture(
                context,
                deterministic_fixture,
                schema,
                actor_identifier=actor_identifier,
            )
        if local_decision is None:
            raise ValueError("autonomous_local semantic decisions require a local model stage")
        return local_decision(
            semantic_persistence=self,
            semantic_context=dict(context),
        )

    def inspect(self, run_id: UUID, call_id: UUID) -> dict[str, Any]:
        with self.uow_factory() as uow:
            return uow.runs.get_semantic_call(run_id, call_id)


__all__ = [
    "HostArtifactResult",
    "SemanticCallService",
    "redact_sensitive",
    "validate_structured_payload",
]
