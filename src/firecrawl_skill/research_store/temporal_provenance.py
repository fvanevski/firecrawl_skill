"""Transactional terminal guard for explicit temporal research obligations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from .temporal_policy import (
    freshness_satisfied,
    has_temporal_obligations,
    publication_in_window,
)

_QUALIFYING_RELATIONSHIPS = frozenset({"supports", "contradicts", "qualifies"})


class TemporalEvidenceError(RuntimeError):
    """Authoritative temporal evidence does not satisfy the ResearchSpec."""


def _packet_passage_chunks(
    packet: Mapping[str, Any],
) -> tuple[dict[UUID, UUID], set[UUID]]:
    passage_to_chunk: dict[UUID, UUID] = {}
    for section in ("passages", "omitted_passages"):
        for item in packet.get(section, ()):
            if not isinstance(item, Mapping) or not item.get("passage_id"):
                continue
            passage_id = UUID(str(item["passage_id"]))
            raw_chunk_id = item.get("chunk_id") or item.get("passage_id")
            passage_to_chunk[passage_id] = UUID(str(raw_chunk_id))

    bound_passages: set[UUID] = set()
    for binding in packet.get("claim_evidence_bindings", ()):
        if not isinstance(binding, Mapping):
            continue
        if str(binding.get("relationship") or "") not in _QUALIFYING_RELATIONSHIPS:
            continue
        for value in binding.get("passage_ids", ()):
            passage_id = UUID(str(value))
            if passage_id not in passage_to_chunk:
                raise TemporalEvidenceError(
                    "qualifying evidence binding references an unknown passage: "
                    f"{passage_id}"
                )
            bound_passages.add(passage_id)
    return passage_to_chunk, bound_passages


def _passage_temporal_rows(
    uow: Any,
    run_id: UUID,
    passage_ids: set[UUID],
    passage_to_chunk: Mapping[UUID, UUID],
) -> dict[UUID, dict[str, Any]]:
    if not passage_ids:
        return {}
    chunk_to_passages: dict[UUID, list[UUID]] = {}
    for passage_id in passage_ids:
        chunk_to_passages.setdefault(passage_to_chunk[passage_id], []).append(passage_id)
    with uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,d.published_at,a.last_modified,a.retrieved_at
                 FROM chunks c
                 JOIN documents d ON d.id=c.document_id
                 JOIN asset_snapshots a ON a.id=d.snapshot_id
                 JOIN research_run_assets rra
                   ON rra.snapshot_id=d.snapshot_id AND rra.run_id=%s
                WHERE c.id=ANY(%s)""",
            (run_id, list(chunk_to_passages)),
        )
        rows = cursor.fetchall()
    by_chunk = {
        UUID(str(row[0])): {
            "published_at": row[1],
            "updated_at": row[2],
            "retrieved_at": row[3],
        }
        for row in rows
    }
    missing = set(chunk_to_passages) - set(by_chunk)
    if missing:
        raise TemporalEvidenceError(
            "one or more current claim-bound passages are outside authoritative "
            f"run corpus: {sorted(map(str, missing))}"
        )
    return {
        passage_id: by_chunk[chunk_id]
        for chunk_id, members in chunk_to_passages.items()
        for passage_id in members
    }


def _freshness_item_evidence(
    uow: Any,
    run_id: UUID,
    requirement_id: str,
) -> tuple[set[UUID], str, str]:
    with uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT item_id
                 FROM coverage_events
                WHERE run_id=%s AND event_type='item_created'
                  AND item_type='freshness_requirement' AND subject_id=%s
                ORDER BY coverage_revision,id LIMIT 1""",
            (run_id, requirement_id),
        )
        created = cursor.fetchone()
        if created is None:
            raise TemporalEvidenceError(
                f"freshness requirement {requirement_id} has no authoritative "
                "coverage item"
            )
        item_id = created[0]
        cursor.execute(
            """SELECT new_status,payload
                 FROM coverage_events
                WHERE run_id=%s AND item_id=%s
                  AND event_type='item_status_changed'
                ORDER BY coverage_revision DESC,id DESC LIMIT 1""",
            (run_id, item_id),
        )
        status_row = cursor.fetchone()
        cursor.execute(
            """SELECT payload
                 FROM coverage_events
                WHERE run_id=%s AND item_id=%s
                  AND event_type='freshness_observed'
                ORDER BY coverage_revision DESC,id DESC LIMIT 1""",
            (run_id, item_id),
        )
        freshness_row = cursor.fetchone()

    status = str(status_row[0]) if status_row is not None else "unassessed"
    status_payload = dict(status_row[1] or {}) if status_row is not None else {}
    freshness_payload = dict(freshness_row[0] or {}) if freshness_row is not None else {}
    freshness = str(freshness_payload.get("freshness_status") or "uncertain")
    passage_ids = {UUID(str(value)) for value in status_payload.get("passage_ids", ())}
    return passage_ids, status, freshness


def assert_temporal_evidence_satisfied(
    uow: Any,
    run_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    """Reject completed terminal admission unless every bounded obligation holds."""
    connection = getattr(uow, "connection", None)
    if connection is None:
        raise TemporalEvidenceError(
            "temporal evidence check requires a transactional PostgreSQL UoW"
        )
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT s.id,s.spec_revision,s.payload
                 FROM research_runs r
                 LEFT JOIN research_specs s
                   ON s.id=r.research_spec_id AND s.run_id=r.id
                WHERE r.id=%s"""
            + (" FOR UPDATE OF s" if for_update else ""),
            (run_id,),
        )
        spec_row = cursor.fetchone()
    if spec_row is None:
        raise TemporalEvidenceError(f"research run is unavailable: {run_id}")
    if spec_row[0] is None:
        return {
            "schema_version": "temporal-evidence-v1",
            "run_id": str(run_id),
            "required": False,
            "status": "not_applicable",
            "reason": "run has no bound research spec",
        }
    spec_revision = int(spec_row[1])
    spec = spec_row[2] or {}
    if not isinstance(spec, Mapping):
        raise TemporalEvidenceError("current bound ResearchSpec payload is invalid")
    if not has_temporal_obligations(spec):
        return {
            "schema_version": "temporal-evidence-v1",
            "run_id": str(run_id),
            "required": False,
            "status": "not_applicable",
            "reason": "research spec declares no bounded temporal obligations",
            "spec_revision": spec_revision,
        }

    packet = uow.evidence_packets.get_evidence_packet(run_id)
    if packet is None:
        raise TemporalEvidenceError("current EvidencePacket is unavailable")
    if UUID(str(packet.research_spec_id)) != UUID(str(spec_row[0])):
        raise TemporalEvidenceError(
            "current EvidencePacket was prepared for a different ResearchSpec revision"
        )
    packet_payload = packet.payload or {}
    passage_to_chunk, bound_passages = _packet_passage_chunks(packet_payload)
    if not bound_passages:
        raise TemporalEvidenceError(
            "current EvidencePacket has no claim-bound passages for temporal qualification"
        )
    temporal_rows = _passage_temporal_rows(
        uow, run_id, bound_passages, passage_to_chunk
    )

    window = spec.get("time_window") or {}
    if isinstance(window, Mapping) and (window.get("start") or window.get("end")):
        outside_window = [
            passage_id
            for passage_id, row in temporal_rows.items()
            if not publication_in_window(row.get("published_at"), window)
        ]
        if outside_window:
            raise TemporalEvidenceError(
                "explicit ResearchSpec time_window has claim-bound evidence without "
                "qualifying publication provenance: "
                f"{sorted(map(str, outside_window))}"
            )

    now = datetime.now(timezone.utc)
    obligations: list[dict[str, Any]] = []
    for requirement in spec.get("freshness_requirements", ()):
        if not isinstance(requirement, Mapping):
            continue
        max_age = requirement.get("max_age_days")
        if max_age is None:
            continue
        requirement_id = str(requirement.get("requirement_id") or "")
        if not requirement_id:
            raise TemporalEvidenceError("freshness requirement has no requirement_id")
        item_passages, status, freshness = _freshness_item_evidence(
            uow, run_id, requirement_id
        )
        if status != "satisfied" or freshness != "satisfied":
            raise TemporalEvidenceError(
                f"freshness requirement {requirement_id} is not authoritatively "
                f"satisfied (status={status}, freshness={freshness})"
            )
        if not item_passages:
            raise TemporalEvidenceError(
                f"freshness requirement {requirement_id} has no exact passage evidence"
            )
        outside = item_passages - bound_passages
        if outside:
            raise TemporalEvidenceError(
                f"freshness requirement {requirement_id} references passages not bound "
                f"by the current EvidencePacket: {sorted(map(str, outside))}"
            )
        item_rows = {passage_id: temporal_rows[passage_id] for passage_id in item_passages}
        satisfied = any(
            freshness_satisfied(
                published_at=row.get("published_at"),
                updated_at=row.get("updated_at"),
                max_age_days=int(max_age),
                now=now,
            )
            for row in item_rows.values()
        )
        if not satisfied:
            raise TemporalEvidenceError(
                f"freshness requirement {requirement_id} has no qualifying publication "
                "or explicit update within its max-age bound"
            )
        obligations.append(
            {
                "requirement_id": requirement_id,
                "max_age_days": int(max_age),
                "passage_ids": sorted(map(str, item_passages)),
            }
        )

    return {
        "schema_version": "temporal-evidence-v1",
        "run_id": str(run_id),
        "required": True,
        "status": "satisfied",
        "spec_revision": spec_revision,
        "packet_revision": int(packet.packet_revision),
        "bound_passage_count": len(bound_passages),
        "freshness_obligations": obligations,
    }


__all__ = ["TemporalEvidenceError", "assert_temporal_evidence_satisfied"]
