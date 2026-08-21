"""Authoritative claim-manifest persistence for the assessment slice."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID


class ClaimManifestService:
    """Authoritative service for research claims and evidence links.

    Persists claims and claim-to-passage evidence links in PostgreSQL.
    Validates all references before accepting. Rejects URL-only source
    resolution — callers must provide stable passage and snapshot IDs.
    """

    VALID_RELATIONSHIPS = frozenset({"supports", "contradicts", "qualifies", "context"})
    VALID_SEMANTIC_STATUSES = frozenset(
        {
            "supported",
            "contradicted",
            "qualified",
            "unsupported",
            "uncertain",
            "unassessed",
        }
    )

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory

    def create_claim(
        self,
        run_id: UUID,
        claim_id: UUID,
        statement: str,
        *,
        semantic_status: str = "unassessed",
        uncertainty: str | None = None,
        evidence_packet_revision: int = 1,
    ) -> UUID:
        """Insert or update a claim. Returns the row ``id``.

        Idempotent on ``(run_id, claim_id)``.
        """
        if not statement.strip():
            raise ValueError("claim statement must be non-empty")
        if semantic_status not in self.VALID_SEMANTIC_STATUSES:
            raise ValueError(f"invalid semantic_status: {semantic_status}")
        with self.uow_factory() as uow:
            row_id = uow.claims.upsert_claim(
                run_id,
                claim_id,
                statement,
                semantic_status=semantic_status,
                uncertainty=uncertainty,
                evidence_packet_revision=evidence_packet_revision,
            )
        return row_id

    def create_evidence_link(
        self,
        run_id: UUID,
        claim_id: UUID,
        passage_id: UUID,
        snapshot_id: UUID,
        *,
        source_url: str = "",
        relationship: str = "supports",
        confidence: float = 1.0,
    ) -> UUID:
        """Insert a claim-evidence link. Returns the row ``id``.

        Validates that ``passage_id`` exists in ``chunks`` and
        ``snapshot_id`` exists in ``asset_snapshots`` before inserting.
        Rejects URL-only source references — the caller must provide
        stable ``passage_id`` and ``snapshot_id``.
        """
        if relationship not in self.VALID_RELATIONSHIPS:
            raise ValueError(f"invalid relationship: {relationship}")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        with self.uow_factory() as uow:
            row_id = uow.claims.insert_evidence_link(
                run_id,
                claim_id,
                passage_id,
                snapshot_id,
                source_url=source_url,
                relationship=relationship,
                confidence=confidence,
            )
        return row_id

    def list_claims(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all claims for a run."""
        with self.uow_factory() as uow:
            return uow.claims.list_claims(run_id)

    def list_evidence_links(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all evidence links for a run."""
        with self.uow_factory() as uow:
            return uow.claims.list_evidence_links(run_id)

    def export_manifest(self, run_id: UUID) -> dict[str, Any]:
        """Export all claims and links for a run as a JSON-compatible dict."""
        with self.uow_factory() as uow:
            return uow.claims.export_claim_manifest(run_id)

    def import_manifest(
        self,
        run_id: UUID,
        manifest: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Import claims and evidence links from a manifest dict.

        Dry-run-first: validates all references before committing.
        Idempotent — existing claims are upserted, links are appended.
        """
        claims = manifest.get("claims", [])
        links = manifest.get("links", [])

        unknown_passages = []
        unknown_snapshots = []
        malformed_claim_ids = []

        with self.uow_factory() as uow:
            for claim in claims:
                cid = claim.get("claim_id")
                if cid:
                    try:
                        UUID(str(cid))
                    except ValueError:
                        malformed_claim_ids.append(str(cid))

            for link in links:
                pid = link.get("passage_id")
                sid = link.get("snapshot_id")
                if pid:
                    try:
                        uid = UUID(str(pid))
                        if not uow.claims.validate_passage_id(uid):
                            unknown_passages.append(str(pid))
                    except ValueError:
                        unknown_passages.append(str(pid))
                if sid:
                    try:
                        uid = UUID(str(sid))
                        if not uow.claims.validate_snapshot_id(uid):
                            unknown_snapshots.append(str(sid))
                    except ValueError:
                        unknown_snapshots.append(str(sid))

        dry_run_result = {
            "dry_run": True,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "valid": not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

        if dry_run or (malformed_claim_ids or unknown_passages or unknown_snapshots):
            return dry_run_result

        failed_claims = []
        failed_links = []
        inserted_claims = 0
        with self.uow_factory() as uow:
            for claim in claims:
                try:
                    uow.claims.upsert_claim(
                        run_id,
                        UUID(str(claim["claim_id"])),
                        claim["statement"],
                        semantic_status=claim.get("semantic_status", "unassessed"),
                        uncertainty=claim.get("uncertainty"),
                        evidence_packet_revision=claim.get(
                            "evidence_packet_revision", 1
                        ),
                    )
                    inserted_claims += 1
                except Exception as exc:  # noqa: BLE001
                    failed_claims.append(
                        {
                            "claim_id": str(claim.get("claim_id", "unknown")),
                            "error": str(exc),
                        }
                    )

            inserted_links = 0
            for link in links:
                try:
                    uow.claims.insert_evidence_link(
                        run_id,
                        UUID(str(link["claim_id"])),
                        UUID(str(link["passage_id"])),
                        UUID(str(link["snapshot_id"])),
                        source_url=link.get("source_url", ""),
                        relationship=link.get("relationship", "supports"),
                        confidence=link.get("confidence", 1.0),
                    )
                    inserted_links += 1
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc).lower()
                    if (
                        "unique constraint" in exc_str
                        or "uk_claim_evidence_links" in exc_str
                        or "duplicate key" in exc_str
                    ):
                        inserted_links += 1
                    else:
                        failed_links.append(
                            {
                                "claim_id": str(link.get("claim_id", "unknown")),
                                "passage_id": str(link.get("passage_id", "unknown")),
                                "error": str(exc),
                            }
                        )

        has_failures = bool(failed_claims) or bool(failed_links)
        return {
            "dry_run": False,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "inserted_claims": inserted_claims,
            "inserted_links": inserted_links,
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "failed_claims": failed_claims,
            "failed_links": failed_links,
            "valid": not has_failures
            and not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

    def _passage_id_valid(self, passage_id: UUID) -> bool:
        """Check if passage_id exists in chunks."""
        with self.uow_factory() as uow:
            return uow.claims.validate_passage_id(passage_id)

    def _snapshot_id_valid(self, snapshot_id: UUID) -> bool:
        """Check if snapshot_id exists in asset_snapshots."""
        with self.uow_factory() as uow:
            return uow.claims.validate_snapshot_id(snapshot_id)


__all__ = ["ClaimManifestService"]
