"""Temporal evidence gate for terminal completion.

Second transactional safety check, layered on top of completion provenance:
before a run is recorded as ``completed``, every required time-bounded
obligation in the authoritative research spec (bounded time window, freshness
requirements) must be satisfied by the temporal provenance of qualifying
evidence.

Temporal authority model:

* publication: ``documents.published_at`` — the only timestamp that can
  satisfy a bounded time-window obligation; a recent update never makes an
  old publication newly published.
* update: ``asset_snapshots.last_modified`` — an ambiguous provider signal
  (HTTP Last-Modified / page text) that may satisfy a freshness
  max-age obligation but never a publication-window obligation.
* retrieval: ``asset_snapshots.retrieved_at`` — never satisfies any
  time-bounded obligation.

Background/context evidence may remain retained and indexed but cannot
satisfy a bounded obligation, and a bounded obligation with no qualifying
temporal evidence is unsatisfied, never not-applicable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain import load_model
from firecrawl_skill.research_domain.models import (
    FreshnessRequirement,
    ResearchSpec,
    TimeWindow,
)

_QUALIFYING_RELATIONSHIPS = frozenset({"supports", "contradicts", "qualifies"})
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TemporalEvidenceError(RuntimeError):
    """A required time-bounded evidence obligation is unsatisfied."""


def _parse_bound(value: str, *, end_of_day: bool = False) -> datetime:
    raw = str(value).strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if _DATE_ONLY.fullmatch(raw):
        if end_of_day:
            # A date-only end includes the whole day: exclusive next-midnight bound.
            return parsed + timedelta(days=1)
        return parsed
    return parsed


def _normalize_publication(value: Any) -> datetime | None:
    """Normalize a temporal timestamp; never fabricates missing values."""
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            # Ambiguous provider update signals (e.g. asset_snapshots
            # last_modified text) may carry HTTP dates.
            try:
                value = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _spec_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise TemporalEvidenceError(
                "research spec payload is not structured JSON"
            ) from exc
    if not isinstance(payload, dict):
        raise TemporalEvidenceError("research spec payload is not structured JSON")
    return payload


def _qualifying_chunk_ids(packet: dict[str, Any]) -> tuple[set[str], list[str]]:
    """Chunk IDs referenced by qualifying (non-context) claim bindings."""
    passages: dict[str, str] = {}
    for section in ("passages", "omitted_passages"):
        for item in packet.get(section) or ():
            passage_id = item.get("passage_id")
            chunk_id = item.get("chunk_id")
            if not passage_id:
                continue
            if chunk_id is None:
                raise TemporalEvidenceError(
                    "evidence passage is missing its authoritative chunk identifier"
                )
            passages[str(passage_id)] = str(chunk_id)
    chunk_ids: set[str] = set()
    unknown: list[str] = []
    for binding in packet.get("claim_evidence_bindings") or ():
        relationship = str(binding.get("relationship") or "")
        if relationship not in _QUALIFYING_RELATIONSHIPS:
            continue
        for passage_id in binding.get("passage_ids") or ():
            key = str(passage_id)
            if key not in passages:
                unknown.append(key)
                continue
            chunk_ids.add(passages[key])
    return chunk_ids, unknown


def assert_temporal_evidence_satisfied(
    uow: Any,
    run_id: UUID,
    *,
    for_update: bool = True,
) -> dict[str, Any]:
    """Fail closed unless every bounded temporal obligation is satisfied.

    Runs inside the caller's UoW transaction so the check shares its locks
    with the terminal decision that motivated it.
    """
    connection = getattr(uow, "connection", None)
    if connection is None:
        raise TemporalEvidenceError(
            "temporal evidence check requires a transactional PostgreSQL UoW"
        )
    with connection.cursor() as cur:
        suffix = " FOR UPDATE OF s" if for_update else ""
        cur.execute(
            """SELECT s.payload,s.spec_revision
                 FROM research_runs r
                 JOIN research_specs s
                   ON s.id=r.research_spec_id AND s.run_id=r.id
                WHERE r.id=%s"""
            + suffix,
            (run_id,),
        )
        spec_row = cur.fetchone()
        if spec_row is None:
            return {
                "schema_version": "temporal-evidence-v1",
                "status": "not_applicable",
                "reason": "run has no bound research spec",
            }
        try:
            spec = load_model(_spec_payload(spec_row[0]))
        except (ValueError, TypeError) as exc:
            raise TemporalEvidenceError(
                f"research spec cannot be loaded for temporal validation: {exc}"
            ) from exc
        if not isinstance(spec, ResearchSpec):
            raise TemporalEvidenceError(
                "bound research spec payload is not a ResearchSpec model"
            )
        spec_revision = int(spec_row[1])

        obligations: list[dict[str, Any]] = []
        window: TimeWindow | None = getattr(spec, "time_window", None)
        if window is not None and (window.start or window.end):
            obligations.append(
                {
                    "kind": "time_window",
                    "requirement_id": None,
                    "start": _parse_bound(window.start) if window.start else None,
                    "end": (
                        _parse_bound(window.end, end_of_day=True)
                        if window.end
                        else None
                    ),
                }
            )
        for requirement in getattr(spec, "freshness_requirements", ()) or ():
            if not isinstance(requirement, FreshnessRequirement):
                continue
            if requirement.max_age_days is None:
                continue
            obligations.append(
                {
                    "kind": "freshness",
                    "requirement_id": str(requirement.requirement_id),
                    "max_age_days": int(requirement.max_age_days),
                }
            )
        if not obligations:
            return {
                "schema_version": "temporal-evidence-v1",
                "status": "not_applicable",
                "reason": "research spec declares no bounded temporal obligations",
                "spec_revision": spec_revision,
            }

        suffix = " FOR UPDATE" if for_update else ""
        cur.execute(
            """SELECT id,packet_revision,payload
                 FROM evidence_packets
                WHERE run_id=%s
                ORDER BY packet_revision DESC
                LIMIT 1"""
            + suffix,
            (run_id,),
        )
        packet_row = cur.fetchone()
        if packet_row is None:
            raise TemporalEvidenceError(
                "temporal obligations exist but the run has no persisted EvidencePacket"
            )
        packet = packet_row[2]
        if not isinstance(packet, dict):
            raise TemporalEvidenceError("EvidencePacket payload is not structured JSON")
        packet_revision = int(packet_row[1])
        chunk_ids, unknown = _qualifying_chunk_ids(packet)
        if unknown:
            raise TemporalEvidenceError(
                "qualifying evidence bindings reference unknown passages: "
                f"{sorted(unknown)}"
            )
        publications: list[datetime] = []
        updates: list[datetime] = []
        if chunk_ids:
            cur.execute(
                """SELECT c.id,d.published_at,a.last_modified
                      FROM chunks c
                      JOIN documents d ON d.id=c.document_id
                      JOIN asset_snapshots a ON a.id=d.snapshot_id
                     WHERE c.id=ANY(%s::uuid[])""",
                (sorted(chunk_ids),),
            )
            corpus_rows = cur.fetchall()
            found = {str(row[0]) for row in corpus_rows}
            if found != chunk_ids:
                raise TemporalEvidenceError(
                    "qualifying evidence references chunks outside the corpus: "
                    f"{sorted(chunk_ids - found)}"
                )
            for _chunk_id, published_at, last_modified in corpus_rows:
                normalized_publication = _normalize_publication(published_at)
                if normalized_publication is not None:
                    publications.append(normalized_publication)
                normalized_update = _normalize_publication(last_modified)
                if normalized_update is not None:
                    updates.append(normalized_update)

        now = datetime.now(timezone.utc)
        unsatisfied: list[str] = []
        evaluated: list[dict[str, Any]] = []
        for obligation in obligations:
            if obligation["kind"] == "time_window":
                # Publication authority only: a recent update never makes an
                # old publication newly published.
                start: datetime | None = obligation["start"]
                end: datetime | None = obligation["end"]
                satisfied = any(
                    (start is None or publication >= start)
                    and (end is None or publication < end)
                    for publication in publications
                )
                detail = {
                    "window_start": start.isoformat() if start else None,
                    "window_end_exclusive": end.isoformat() if end else None,
                }
                evidence = publications
            else:
                # Freshness max-age: an authoritative publication or a
                # domain-compatible update signal within the cutoff qualifies.
                cutoff = now - timedelta(days=obligation["max_age_days"])
                satisfied = any(
                    timestamp >= cutoff for timestamp in (*publications, *updates)
                )
                detail = {"max_age_days": obligation["max_age_days"]}
                evidence = (*publications, *updates)
            entry = {
                "kind": obligation["kind"],
                "requirement_id": obligation.get("requirement_id"),
                "satisfied": satisfied,
                **detail,
            }
            evaluated.append(entry)
            if not satisfied:
                label = (
                    obligation["kind"]
                    if obligation.get("requirement_id") is None
                    else f"{obligation['kind']}:{obligation['requirement_id']}"
                )
                if not evidence:
                    unsatisfied.append(
                        f"{label}: no qualifying evidence carries a "
                        f"{obligation['kind']} temporal timestamp"
                    )
                else:
                    unsatisfied.append(
                        f"{label}: no qualifying temporal timestamp within "
                        f"the required bounds ({detail})"
                    )
        if unsatisfied:
            raise TemporalEvidenceError(
                "terminal completion is blocked by unsatisfied temporal "
                f"evidence obligations: {'; '.join(unsatisfied)}"
            )
        return {
            "schema_version": "temporal-evidence-v1",
            "status": "satisfied",
            "spec_revision": spec_revision,
            "packet_revision": packet_revision,
            "qualifying_publication_count": len(publications),
            "qualifying_update_count": len(updates),
            "obligations": evaluated,
        }
