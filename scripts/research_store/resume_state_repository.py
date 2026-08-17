"""Concrete PostgreSQL implementation of the ResumeStatePort.

This module provides a read-only reader that coordinates canonical
Phase-3 repositories via the unit-of-work to satisfy the narrow state
queries needed by the resume lifecycle.  It contains no workflow policy.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .orchestration.ports import ResumeCounts


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

    def authorized_queries(self, run_id: UUID) -> list[dict[str, Any]]:
        with self._uow_factory() as uow:
            proposals = uow.strategy_revisions.list_proposals(run_id, limit=10_000)
            results: list[dict[str, Any]] = []
            for proposal in proposals:
                if proposal["decision_type"] != "search":
                    continue
                queries = proposal.get("proposed_queries") or []
                if not queries:
                    continue
                decision_ids = uow.strategy_revisions.list_decision_ids_for_proposal(
                    run_id, proposal["proposal_id"]
                )
                accepted = False
                for decision_id in decision_ids:
                    decision = uow.strategy_revisions.get_decision(run_id, decision_id)
                    if decision and decision.get("outcome") == "accepted":
                        accepted = True
                        break
                if not accepted:
                    continue
                results.append(
                    {
                        "proposal_id": proposal["proposal_id"],
                        "decision_type": "search",
                        "proposed_queries": list(queries),
                    }
                )
        return results

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
            from .smart_orchestrator import SmartResumeError

            raise SmartResumeError("synthesizing run has no EvidencePacket")
        return packet.packet_revision
