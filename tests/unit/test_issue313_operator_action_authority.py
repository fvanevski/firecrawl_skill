from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from firecrawl_skill.research_store.operator_action_service import (
    OperatorActionError,
    OperatorActionService,
)


class _Runs:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[tuple[int, int]] = []

    def list_events(
        self,
        _run_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((limit, offset))
        return self.events[offset : offset + limit]


class _Uow:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.runs = _Runs(events)


def _event(sequence: int, event_type: str, payload: dict[str, Any] | None = None):
    return {
        "sequence_number": sequence,
        "event_type": event_type,
        "payload": payload or {},
    }


def test_temporal_scope_authority_reads_beyond_first_event_page() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    events = [_event(index, "unrelated.event") for index in range(1, 151)]
    gap = {
        "kind": "temporal_coverage_gap",
        "coverage_revision": 7,
        "reason": "late authoritative gap",
    }
    events.append(
        _event(
            151,
            "evidence.temporal_coverage_gap",
            {"temporal_coverage_gap": gap},
        )
    )
    uow = _Uow(events)

    assert OperatorActionService._active_temporal_gap(uow, run_id) == gap
    assert uow.runs.calls == [(100, 0), (100, 100)]


def test_later_temporal_resolution_clears_gap_across_pages() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000002")
    gap = {
        "kind": "temporal_coverage_gap",
        "coverage_revision": 3,
        "reason": "gap before later resolution",
    }
    events = [
        _event(
            1,
            "evidence.temporal_coverage_gap",
            {"temporal_coverage_gap": gap},
        )
    ]
    events.extend(_event(index, "unrelated.event") for index in range(2, 151))
    events.append(_event(151, "evidence.temporal_coverage_resolved"))
    uow = _Uow(events)

    assert OperatorActionService._active_temporal_gap(uow, run_id) is None
    assert uow.runs.calls == [(100, 0), (100, 100)]


def test_malformed_temporal_gap_event_fails_closed() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000003")
    uow = _Uow(
        [
            _event(
                1,
                "evidence.temporal_coverage_gap",
                {"temporal_coverage_gap": "not-an-object"},
            )
        ]
    )

    with pytest.raises(OperatorActionError, match="temporal coverage gap is malformed"):
        OperatorActionService._active_temporal_gap(uow, run_id)
