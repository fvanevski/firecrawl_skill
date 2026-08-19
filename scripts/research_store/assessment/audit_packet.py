"""Deterministic audit packet identity from authoritative PostgreSQL state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from uuid import UUID


def compute_audit_packet_hash_from_db(run_id: UUID, uow_factory: Callable) -> str:
    """Hash the complete authoritative run projection used for semantic audit."""
    with uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            "SELECT row_to_json(r) FROM research_runs r WHERE r.id=%s",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"research run {run_id} not found")
        run = row[0]

        def rows(query: str) -> list[dict]:
            cur.execute(query, (run_id,))
            return [item[0] for item in cur.fetchall()]

        invocations = rows(
            """SELECT row_to_json(i) FROM research_invocations i
                   WHERE i.run_id=%s ORDER BY i.created_at,i.id"""
        )
        events = rows(
            """SELECT row_to_json(e) FROM research_events e
                   WHERE e.run_id=%s ORDER BY e.sequence_number,e.id"""
        )
        claims = rows(
            """SELECT row_to_json(c) FROM research_claims c
                   WHERE c.run_id=%s ORDER BY c.created_at,c.id"""
        )
        evidence = rows(
            """SELECT row_to_json(l) FROM claim_evidence_links l
                   WHERE l.run_id=%s ORDER BY l.created_at,l.id"""
        )
        assets = rows(
            """SELECT row_to_json(a) FROM research_run_assets a
                   WHERE a.run_id=%s ORDER BY a.snapshot_id,a.role"""
        )
        coverage = rows(
            """SELECT row_to_json(c) FROM coverage_snapshots c
                   WHERE c.run_id=%s ORDER BY c.coverage_revision,c.id"""
        )
        assessments = rows(
            """SELECT row_to_json(a) FROM audit_assessments a
                   WHERE a.run_id=%s ORDER BY a.created_at,a.id"""
        )

    packet = {
        "schema_version": "audit-packet-v2",
        "run": run,
        "invocations": invocations,
        "events": events,
        "claims": claims,
        "claim_evidence_links": evidence,
        "run_assets": assets,
        "coverage_snapshots": coverage,
        "assessments": assessments,
    }
    payload = json.dumps(
        packet,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["compute_audit_packet_hash_from_db"]
