"""Authoritative release verifier with comparison-bound timing validation.

The established verifier remains the implementation core. This entry point
injects the versioned timing-evidence contract and is the only verifier invoked
by the release workflow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import verify_release_campaign as _base
from release_campaign_timing_contract import (
    validate_timing_diagnostics as _validate_timing,
)

_BASE_VERIFY = _base.verify
_ACTIVE_COMPARISON: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "release_campaign_comparison",
    default=None,
)

WorkflowIdentity = _base.WorkflowIdentity
_database_completion = _base._database_completion


def validate_timing_diagnostics(
    diagnostics: Mapping[str, Any],
    *,
    candidate_sha: str,
    run_ids: Sequence[str],
    comparison: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate timing evidence against the active comparison artifact."""
    effective = comparison if comparison is not None else _ACTIVE_COMPARISON.get()
    return _validate_timing(
        diagnostics,
        candidate_sha=candidate_sha,
        run_ids=run_ids,
        comparison=effective,
    )


def _timing_adapter(
    diagnostics: Mapping[str, Any],
    *,
    candidate_sha: str,
    run_ids: Sequence[str],
) -> list[str]:
    return validate_timing_diagnostics(
        diagnostics,
        candidate_sha=candidate_sha,
        run_ids=run_ids,
    )


def _comparison_for(root: Path) -> Mapping[str, Any] | None:
    paths = sorted((root / "reproducibility").glob("*/comparison.json"))
    if len(paths) != 1:
        return None
    return _base.load_object(paths[0])


def verify(
    *,
    root: Path,
    dataset_path: Path,
    database_url: str,
    identity: WorkflowIdentity,
    execution_conclusion: str,
) -> tuple[dict[str, Any], list[str]]:
    """Run the established verifier with strict timing cross-validation active."""
    token = _ACTIVE_COMPARISON.set(_comparison_for(root))
    original_validator = _base.validate_timing_diagnostics
    _base.validate_timing_diagnostics = _timing_adapter
    try:
        return _BASE_VERIFY(
            root=root,
            dataset_path=dataset_path,
            database_url=database_url,
            identity=identity,
            execution_conclusion=execution_conclusion,
        )
    finally:
        _base.validate_timing_diagnostics = original_validator
        _ACTIVE_COMPARISON.reset(token)


def main(argv: list[str] | None = None) -> int:
    """Delegate argument parsing/output to the established CLI implementation."""
    original_verify = _base.verify
    _base.verify = verify
    try:
        return _base.main(argv)
    finally:
        _base.verify = original_verify


def __getattr__(name: str) -> Any:
    return getattr(_base, name)


if __name__ == "__main__":
    raise SystemExit(main())
