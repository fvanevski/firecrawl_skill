"""Explicit semantic-authority policy for research execution modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


SUPPORTED_EXECUTION_MODES = frozenset(
    {"agent_led", "autonomous_local", "deterministic_debug"}
)


class ExecutionModeError(ValueError):
    """An execution-mode request violates the configured authority boundary."""


class SemanticAuthority(str, Enum):
    HOST_AGENT = "host-agent"
    LOCAL_MODEL = "local-model"
    DETERMINISTIC_FIXTURE = "deterministic-fixture"


@dataclass(frozen=True)
class ExecutionModePolicy:
    """Resolve one persisted execution mode to exactly one semantic authority."""

    version: str = "execution-mode-v1"

    def validate_mode(self, mode: str) -> str:
        if mode not in SUPPORTED_EXECUTION_MODES:
            raise ExecutionModeError(f"unsupported execution mode: {mode}")
        return mode

    def authority_for(self, mode: str) -> SemanticAuthority:
        self.validate_mode(mode)
        return {
            "agent_led": SemanticAuthority.HOST_AGENT,
            "autonomous_local": SemanticAuthority.LOCAL_MODEL,
            "deterministic_debug": SemanticAuthority.DETERMINISTIC_FIXTURE,
        }[mode]

    def authorize(self, mode: str, authority: SemanticAuthority) -> None:
        expected = self.authority_for(mode)
        if authority != expected:
            raise ExecutionModeError(
                f"execution mode {mode} requires {expected.value} semantic authority; "
                f"received {authority.value}"
            )

    def route(
        self,
        mode: str,
        *,
        host_artifact_supplied: bool = False,
        deterministic_fixture_supplied: bool = False,
    ) -> SemanticAuthority:
        authority = self.authority_for(mode)
        if host_artifact_supplied and deterministic_fixture_supplied:
            raise ExecutionModeError("only one supplied semantic artifact is permitted")
        if authority == SemanticAuthority.HOST_AGENT and not host_artifact_supplied:
            raise ExecutionModeError(
                "agent_led semantic decisions require a host-agent artifact"
            )
        if (
            authority == SemanticAuthority.DETERMINISTIC_FIXTURE
            and not deterministic_fixture_supplied
        ):
            raise ExecutionModeError(
                "deterministic_debug semantic decisions require a deterministic fixture"
            )
        if authority == SemanticAuthority.LOCAL_MODEL and (
            host_artifact_supplied or deterministic_fixture_supplied
        ):
            supplied = "host-agent" if host_artifact_supplied else "deterministic-fixture"
            raise ExecutionModeError(
                f"autonomous_local semantic decisions cannot use {supplied} authority"
            )
        return authority


__all__ = [
    "ExecutionModeError",
    "ExecutionModePolicy",
    "SUPPORTED_EXECUTION_MODES",
    "SemanticAuthority",
]
