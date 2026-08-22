"""Canonical fscrape identity plus replay-safe hard-budget composition."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .acquisition.replay_safe_direct_scrape import ReplaySafeDirectScrapeService
from .fscrape_contract import FScrapeRequest, FScrapeResult, new_invocation_id
from .fscrape_service import FScrapeService, ValidatedDirectScrapeService


class ReplaySafeValidatedDirectScrapeService(
    ValidatedDirectScrapeService,
    ReplaySafeDirectScrapeService,
):
    """Combine structured validation with replay-safe budget admission."""


@dataclass(frozen=True)
class CanonicalFScrapeResult(FScrapeResult):
    """Add an explicit operator-visible fresh/replay classification."""

    fresh_requested: bool = False
    fresh_effective: bool = False
    fresh_parent_invocation_id: UUID | None = None

    def to_dict(self):
        value = super().to_dict()
        value["fresh_requested"] = self.fresh_requested
        value["fresh_effective"] = self.fresh_effective
        value["fresh_parent_invocation_id"] = (
            str(self.fresh_parent_invocation_id)
            if self.fresh_parent_invocation_id is not None
            else None
        )
        value["work_mode"] = (
            "replay"
            if self.batch.replayed
            else "fresh"
            if self.fresh_effective
            else "new"
        )
        return value


class CanonicalFScrapeService(FScrapeService):
    """Use the direct-scrape service's canonical normalized logical identity."""

    direct_service: ReplaySafeDirectScrapeService

    def execute(self, request: FScrapeRequest) -> FScrapeResult:
        run_status = self._resolve_run(request.research_run_id)
        run_id = UUID(str(run_status.id))
        requested_external_id = request.external_invocation_id or new_invocation_id()
        direct_requests = request.direct_requests()
        logical_key = self.direct_service.logical_idempotency_key(
            run_id, direct_requests
        )
        fresh_effective = request.fresh and request.idempotency_key is None
        fresh_parent: UUID | None = None
        if request.idempotency_key is not None:
            key = request.idempotency_key
        elif fresh_effective:
            from .fscrape_service import fresh_idempotency_key

            key = fresh_idempotency_key(run_id, requested_external_id)
            fresh_parent = self.direct_service.latest_terminal_logical_invocation(
                run_id,
                direct_requests,
                exclude_idempotency_key=key,
            )
        else:
            key = logical_key
        batch = self.direct_service.execute(
            run_id,
            direct_requests,
            idempotency_key=key,
            external_invocation_id=requested_external_id,
            parent_invocation_id=fresh_parent,
        )
        if fresh_effective:
            # The invocation row is immutable authority for lineage. A replay of
            # the same fresh identity must report its originally persisted parent,
            # not a newly computed latest logical invocation.
            fresh_parent = self.direct_service.invocation_parent(
                run_id, batch.invocation_id
            )
            self.direct_service.record_fresh_invocation_lineage(
                run_id,
                batch.invocation_id,
                logical_idempotency_key=logical_key,
                parent_invocation_id=fresh_parent,
            )
        authoritative_external_id = self._authoritative_external_invocation_id(batch)
        return CanonicalFScrapeResult(
            research_run_id=request.research_run_id,
            external_invocation_id=authoritative_external_id,
            batch=batch,
            index_job_ids_by_chunk=self._index_job_ids(batch),
            fresh_requested=request.fresh,
            fresh_effective=fresh_effective,
            fresh_parent_invocation_id=fresh_parent,
        )


__all__ = [
    "CanonicalFScrapeResult",
    "CanonicalFScrapeService",
    "ReplaySafeValidatedDirectScrapeService",
]
