"""Concrete PostgreSQL implementation of the ResumeStatePort.

This module provides a read-only reader that coordinates canonical Phase-3
repositories via the unit of work to satisfy the narrow state queries needed
by the resume lifecycle. It contains no workflow policy and no raw SQL.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .orchestration.ports import ResumeCounts
from .smart_result import AcquisitionAttemptCensus

_MAX_CENSUS_ATTEMPTS = 1000
_MAX_UNSUCCESSFUL_DETAILS = 50
_MAX_RESUME_EVENTS = 10000
_EVENT_PAGE_SIZE = 100
_TEMPORAL_GAP_EVENT = "evidence.temporal_coverage_gap"
_TEMPORAL_RESOLVED_EVENT = "evidence.temporal_coverage_resolved"


class PostgresResumeStateReader:
    """Read-only PostgreSQL adapter implementing ``ResumeStatePort``."""

    def __init__(self, uow_factory: Any) -> None:
        self._uow_factory = uow_factory

    def counts(self, run_id: UUID) -> ResumeCounts:
        with self._uow_factory() as uow:
            waves = uow.runs.count_acquisition_waves(run_id)
            attempts = uow.extraction_attempts.count_for_run(run_id)
            assets = uow.snapshots.count_run_assets(run_id)
        return ResumeCounts(
            waves=waves,
            attempts=attempts,
            assets=assets,
        )

    def attempt_census(self, run_id: UUID) -> AcquisitionAttemptCensus:
        """Build one durable extraction-attempt census without replay side effects."""
        with self._uow_factory() as uow:
            attempted = int(uow.extraction_attempts.count_for_run(run_id))
            if attempted > _MAX_CENSUS_ATTEMPTS:
                raise ValueError(
                    "run extraction-attempt census exceeds the supported bounded size"
                )
            attempts = uow.extraction_attempts.list_attempts_for_run(
                run_id,
                limit=_MAX_CENSUS_ATTEMPTS,
                offset=0,
            )
            if len(attempts) != attempted:
                raise ValueError(
                    "run extraction-attempt census changed during authoritative read"
                )

            attempts.sort(
                key=lambda item: (
                    str(item.get("start_time") or ""),
                    str(item.get("id") or ""),
                )
            )
            succeeded = 0
            failure_counts: dict[str, int] = {}
            unsuccessful_details: list[dict[str, Any]] = []
            for attempt in attempts:
                if str(attempt.get("exit_status") or "") == "succeeded":
                    succeeded += 1
                    continue

                failure_class = str(
                    attempt.get("failure_class")
                    or attempt.get("exit_status")
                    or "unknown"
                )
                failure_counts[failure_class] = failure_counts.get(failure_class, 0) + 1
                if len(unsuccessful_details) >= _MAX_UNSUCCESSFUL_DETAILS:
                    continue
                candidate_id = attempt.get("candidate_id")
                target_url = None
                if candidate_id:
                    try:
                        candidate = uow.candidates.get_candidate(
                            candidate_id, run_id=run_id
                        )
                    except (KeyError, ValueError):
                        candidate = None
                    if candidate:
                        target_url = candidate.get("canonical_url") or candidate.get(
                            "original_url"
                        )
                unsuccessful_details.append(
                    {
                        "attempt_id": str(attempt.get("id") or ""),
                        "candidate_id": (
                            str(candidate_id) if candidate_id is not None else None
                        ),
                        "target_url": target_url,
                        "exit_status": str(attempt.get("exit_status") or "unknown"),
                        "failure_class": failure_class,
                    }
                )

        unsuccessful = attempted - succeeded
        return AcquisitionAttemptCensus(
            attempted=attempted,
            succeeded=succeeded,
            unsuccessful=unsuccessful,
            failure_counts=dict(sorted(failure_counts.items())),
            unsuccessful_attempts=tuple(unsuccessful_details),
        )

    def authorized_queries(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return accepted search proposals in persisted ascending revision order."""
        with self._uow_factory() as uow:
            return uow.strategy_revisions.list_accepted_search_proposals(run_id)

    def completed_candidates(self, run_id: UUID) -> set[str]:
        with self._uow_factory() as uow:
            return uow.snapshots.completed_candidate_ids(run_id)

    def assets(self, run_id: UUID) -> list[dict[str, Any]]:
        with self._uow_factory() as uow:
            rows = uow.snapshots.resume_assets_for_run(run_id)
        return [
            {
                "status": "complete",
                "ordinal": index,
                "requested_url": row[3],
                "snapshot_id": str(row[2]),
                "chunk_ids": [str(chunk_id) for chunk_id in row[4]],
                "candidate_id": str(row[1]),
                "extraction_attempt_id": str(row[0]),
                "resume_replay": True,
            }
            for index, row in enumerate(rows)
        ]

    def packet_revision(self, run_id: UUID) -> int:
        with self._uow_factory() as uow:
            packet = uow.evidence_packets.get_evidence_packet(run_id)
        if packet is None:
            from .orchestration.resume_support import SmartResumeError

            raise SmartResumeError("synthesizing run has no EvidencePacket")
        return packet.packet_revision

    def temporal_coverage_gap(self, run_id: UUID) -> dict[str, Any] | None:
        """Resolve the active gap from the immutable event journal.

        The bounded scan fails closed rather than silently treating a truncated
        journal as authoritative. A later resolution event clears an older gap.
        """

        latest_gap: dict[str, Any] | None = None
        latest_gap_sequence = -1
        latest_resolution_sequence = -1
        offset = 0
        with self._uow_factory() as uow:
            while offset < _MAX_RESUME_EVENTS:
                limit = min(_EVENT_PAGE_SIZE, _MAX_RESUME_EVENTS - offset)
                events = uow.runs.list_events(
                    run_id,
                    limit=limit,
                    offset=offset,
                )
                for event in events:
                    sequence = int(event.get("sequence_number") or 0)
                    if event.get("event_type") == _TEMPORAL_GAP_EVENT:
                        payload = event.get("payload") or {}
                        gap = payload.get("temporal_coverage_gap")
                        if not isinstance(gap, dict):
                            raise ValueError(
                                "persisted temporal coverage gap event is malformed"
                            )
                        if sequence > latest_gap_sequence:
                            latest_gap = dict(gap)
                            latest_gap_sequence = sequence
                    elif event.get("event_type") == _TEMPORAL_RESOLVED_EVENT:
                        latest_resolution_sequence = max(
                            latest_resolution_sequence, sequence
                        )
                offset += len(events)
                if len(events) < limit:
                    break
            else:
                raise ValueError(
                    "run event history exceeds bounded temporal-gap resume scan"
                )

        if latest_gap is None or latest_resolution_sequence > latest_gap_sequence:
            return None
        return latest_gap
