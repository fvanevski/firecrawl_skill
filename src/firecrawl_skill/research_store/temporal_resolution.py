"""Bounded deterministic post-acquisition temporal provenance resolution.

The resolver consumes the canonical temporal signals already extracted during
corpus ingestion. It does not run open-ended NLP or perform provider search.
Source-specific semantics are admitted only after the extractor has established
an exact supported source identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .temporal_candidate import parse_provider_datetime

TEMPORAL_RESOLUTION_SCHEMA_VERSION = "temporal-provenance-resolution-v1"
MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN = 32
MAX_TEMPORAL_PROVENANCE_PROBES_PER_DOCUMENT = 1
MAX_SOURCE_SPECIFIC_ACTIONS_PER_DOCUMENT = 1


def _valid_values(
    signals: Sequence[Mapping[str, Any]], *, source: str | None = None
) -> list[datetime]:
    values: list[datetime] = []
    for signal in signals:
        if source is not None and signal.get("source") != source:
            continue
        if signal.get("status") != "valid":
            continue
        parsed = parse_provider_datetime(signal.get("parsed") or signal.get("raw"))
        if parsed is not None:
            values.append(parsed.astimezone(timezone.utc))
    return values


def _status(values: Sequence[datetime]) -> tuple[str, str | None]:
    distinct = sorted(set(values))
    if not distinct:
        return "unknown", None
    if len(distinct) > 1:
        return "explicit_conflict", None
    return "explicit_valid", distinct[0].isoformat()


def resolve_document_temporal_provenance(
    document: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    run_probe_ordinal: int,
) -> dict[str, Any]:
    """Resolve one document once under explicit run/document/action caps.

    ``run_probe_ordinal`` is assigned from persisted run-scoped resolution
    events by the caller. Ordinals beyond the run cap execute no resolution
    stages and return a typed exhausted result.
    """

    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    else:
        retrieved_at = retrieved_at.astimezone(timezone.utc)
    if run_probe_ordinal < 1:
        raise ValueError("run_probe_ordinal must be positive")

    limits = {
        "max_probes_per_run": MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN,
        "max_probes_per_document": MAX_TEMPORAL_PROVENANCE_PROBES_PER_DOCUMENT,
        "max_source_specific_actions_per_document": (
            MAX_SOURCE_SPECIFIC_ACTIONS_PER_DOCUMENT
        ),
    }
    if run_probe_ordinal > MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN:
        return {
            "schema_version": TEMPORAL_RESOLUTION_SCHEMA_VERSION,
            "probe_ordinal": run_probe_ordinal,
            "attempted": False,
            "attempt_count": 0,
            "source_specific_action_count": 0,
            "attempts": [],
            "limits": limits,
            "exhausted": True,
            "exhaustion_reason": "run_probe_budget_exhausted",
            "event_at": None,
            "event_status": "unknown",
            "event_authority": "none",
            "state_observed_at": None,
            "state_authority": "none",
        }

    publications = document.get("publication_signals") or []
    updates = document.get("update_signals") or []
    publications = [item for item in publications if isinstance(item, Mapping)]
    updates = [item for item in updates if isinstance(item, Mapping)]
    source_semantics = document.get("source_semantics") or {}
    if not isinstance(source_semantics, Mapping):
        source_semantics = {}
    source_kind = str(source_semantics.get("source_kind") or "generic")

    canonical_signal_count = sum(
        1
        for item in (*publications, *updates)
        if item.get("source") not in {"http_header", "github_issue_pr_opened_marker"}
    )
    transport_signal_count = sum(
        1 for item in updates if item.get("source") == "http_header"
    )
    attempts: list[dict[str, Any]] = [
        {
            "stage": "canonical_payload",
            "status": "resolved" if canonical_signal_count else "no_signal",
            "signal_count": canonical_signal_count,
        },
        {
            "stage": "transport_metadata",
            "status": "resolved" if transport_signal_count else "no_signal",
            "signal_count": transport_signal_count,
        },
    ]

    event_status = "unknown"
    event_at: str | None = None
    event_authority = "none"
    state_observed_at: str | None = None
    state_authority = "none"
    source_specific_actions = 0

    if source_kind == "github_issue_or_pr":
        source_specific_actions = 1
        opened = _valid_values(
            publications, source="github_issue_pr_opened_marker"
        )
        event_status, event_at = _status(opened)
        if event_status == "explicit_valid":
            event_authority = "github_issue_pr_opened"
        state_observed_at = retrieved_at.isoformat()
        state_authority = "github_issue_pr_snapshot_observation"
        attempts.append(
            {
                "stage": "source_specific",
                "source_kind": source_kind,
                "status": "resolved",
                "event_status": event_status,
                "state_observation": True,
            }
        )
    else:
        attempts.append(
            {
                "stage": "source_specific",
                "source_kind": source_kind,
                "status": "not_applicable",
                "event_status": "unknown",
                "state_observation": False,
            }
        )

    return {
        "schema_version": TEMPORAL_RESOLUTION_SCHEMA_VERSION,
        "probe_ordinal": run_probe_ordinal,
        "attempted": True,
        "attempt_count": 1,
        "source_specific_action_count": source_specific_actions,
        "attempts": attempts,
        "limits": limits,
        "exhausted": True,
        "exhaustion_reason": "bounded_resolution_pass_complete",
        "event_at": event_at,
        "event_status": event_status,
        "event_authority": event_authority,
        "state_observed_at": state_observed_at,
        "state_authority": state_authority,
    }


__all__ = [
    "MAX_SOURCE_SPECIFIC_ACTIONS_PER_DOCUMENT",
    "MAX_TEMPORAL_PROVENANCE_PROBES_PER_DOCUMENT",
    "MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN",
    "TEMPORAL_RESOLUTION_SCHEMA_VERSION",
    "resolve_document_temporal_provenance",
]
