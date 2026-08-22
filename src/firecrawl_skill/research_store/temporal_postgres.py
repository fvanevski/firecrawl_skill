"""Issue-300 PostgreSQL authority seams on the canonical shared UoW.

The subclass keeps the existing transaction owner and repository topology but
strengthens two repositories before the UoW returns to application code:

* candidate temporal metadata is canonicalized in the same transaction that
  materializes search candidates;
* EvidencePacket revision inserts take the research-run row lock so they
  serialize with terminal completion and cannot race its provenance checks.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .postgres import PostgresUnitOfWork
from .postgres_acquisition import (
    PostgresCandidateRepository,
    PostgresSearchAcquisitionRepository,
)
from .postgres_evidence import PostgresEvidencePacketRepository
from .postgres_research import _lock_workflow_run
from .postgres_uow_core import PostgresRepositoryView
from .temporal_candidate import canonical_candidate_temporal


class TemporalCandidateRepository(PostgresCandidateRepository):
    """Canonicalize provider dates before the candidate transaction commits."""

    def __init__(self, connection: Any, search_repository: Any) -> None:
        super().__init__(connection, search_repository)
        self._temporal_connection = connection

    def record_response_candidates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        occurrences = super().record_response_candidates(*args, **kwargs)
        run_id = UUID(str(args[0] if args else kwargs["run_id"]))
        with self._temporal_connection.cursor() as cursor:
            for occurrence in occurrences:
                candidate_id = UUID(str(occurrence["candidate_id"]))
                raw_value = occurrence.get("raw_item") or {}
                raw = dict(raw_value) if isinstance(raw_value, dict) else {}
                cursor.execute(
                    """SELECT published_at,date_signals FROM search_candidates
                         WHERE id=%s AND run_id=%s FOR UPDATE""",
                    (candidate_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError(
                        f"materialized search candidate disappeared: {candidate_id}"
                    )
                publication, signals = canonical_candidate_temporal(
                    raw,
                    stored_publication=row[0],
                    stored_signals=row[1] or {},
                )
                cursor.execute(
                    """UPDATE search_candidates
                          SET published_at=%s,date_signals=%s::jsonb
                        WHERE id=%s AND run_id=%s""",
                    (
                        publication,
                        json.dumps(signals, sort_keys=True),
                        candidate_id,
                        run_id,
                    ),
                )
        return occurrences


class RunLockedEvidencePacketRepository(PostgresEvidencePacketRepository):
    """Serialize packet revision writes with lifecycle/terminal authority."""

    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self._packet_connection = connection

    def persist_evidence_packet(self, run_id: UUID, *args: Any, **kwargs: Any) -> UUID:
        with self._packet_connection.cursor() as cursor:
            _lock_workflow_run(cursor, run_id)
        return super().persist_evidence_packet(run_id, *args, **kwargs)


class TemporalPostgresUnitOfWork(PostgresUnitOfWork):
    """Strengthen issue-300 repository roles without changing UoW ownership."""

    def __enter__(self) -> TemporalPostgresUnitOfWork:
        entered = super().__enter__()
        connection = self.connection
        if connection is None:
            raise RuntimeError("PostgresUnitOfWork entered without a connection")
        search_repository = PostgresSearchAcquisitionRepository(connection)
        candidate_repository = TemporalCandidateRepository(connection, search_repository)
        self.candidates = PostgresRepositoryView(
            "candidates", connection, candidate_repository
        )
        self.evidence_packets = PostgresRepositoryView(
            "evidence_packets",
            connection,
            RunLockedEvidencePacketRepository(connection),
        )
        return entered


__all__ = [
    "RunLockedEvidencePacketRepository",
    "TemporalCandidateRepository",
    "TemporalPostgresUnitOfWork",
]
