"""Read-only PostgreSQL resolver for authoritative corpus identity domains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


class CorpusIdentityResolutionError(LookupError):
    """An identifier is absent or ambiguous across authoritative identity tables."""


@dataclass(frozen=True)
class CorpusIdentityResolution:
    identifier: UUID
    identity_type: str
    run_ids: tuple[UUID, ...] = ()
    promotion_subject_ids: tuple[UUID, ...] = ()
    search_candidate_ids: tuple[UUID, ...] = ()
    extraction_attempt_ids: tuple[UUID, ...] = ()
    source_ids: tuple[UUID, ...] = ()
    snapshot_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    derivation_ids: tuple[UUID, ...] = ()
    chunk_ids: tuple[UUID, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": str(self.identifier),
            "identity_type": self.identity_type,
            "related_ids": {
                "run": [str(value) for value in self.run_ids],
                "promotion_subject": [
                    str(value) for value in self.promotion_subject_ids
                ],
                "search_candidate": [str(value) for value in self.search_candidate_ids],
                "extraction_attempt": [
                    str(value) for value in self.extraction_attempt_ids
                ],
                "source": [str(value) for value in self.source_ids],
                "snapshot": [str(value) for value in self.snapshot_ids],
                "document": [str(value) for value in self.document_ids],
                "derivation": [str(value) for value in self.derivation_ids],
                "chunk": [str(value) for value in self.chunk_ids],
            },
        }


def _uuid_tuple(values: Any) -> tuple[UUID, ...]:
    return tuple(UUID(str(value)) for value in (values or ()))


def resolve_corpus_identity(connection: Any, identifier: UUID | str) -> CorpusIdentityResolution:
    """Resolve one UUID without inferring identity from coincidental UUID equality.

    Every supported identity domain is probed explicitly.  Ambiguous membership is
    rejected instead of assigning precedence.  The related-ID crosswalk is then
    derived only from PostgreSQL foreign-key/provenance relationships.
    """

    value = UUID(str(identifier))
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT identity_type FROM (
                   SELECT 'promotion_subject'::text AS identity_type
                    WHERE EXISTS(SELECT 1 FROM run_asset_promotion_subjects WHERE id=%s)
                   UNION ALL
                   SELECT 'search_candidate'
                    WHERE EXISTS(SELECT 1 FROM search_candidates WHERE id=%s)
                   UNION ALL
                   SELECT 'extraction_attempt'
                    WHERE EXISTS(SELECT 1 FROM extraction_attempts WHERE id=%s)
                   UNION ALL
                   SELECT 'source' WHERE EXISTS(SELECT 1 FROM sources WHERE id=%s)
                   UNION ALL
                   SELECT 'snapshot' WHERE EXISTS(SELECT 1 FROM asset_snapshots WHERE id=%s)
                   UNION ALL
                   SELECT 'document' WHERE EXISTS(SELECT 1 FROM documents WHERE id=%s)
                   UNION ALL
                   SELECT 'derivation'
                    WHERE EXISTS(SELECT 1 FROM document_derivations WHERE id=%s)
                   UNION ALL
                   SELECT 'chunk' WHERE EXISTS(SELECT 1 FROM chunks WHERE id=%s)
               ) identities ORDER BY identity_type""",
            (value,) * 8,
        )
        kinds = [str(row[0]) for row in cursor.fetchall()]
        if not kinds:
            raise CorpusIdentityResolutionError(f"corpus identity not found: {value}")
        if len(kinds) != 1:
            raise CorpusIdentityResolutionError(
                f"ambiguous corpus identity {value}: {','.join(kinds)}"
            )
        kind = kinds[0]
        cursor.execute(
            """WITH base AS (
                   SELECT ps.run_id,ps.candidate_id,ps.snapshot_id,NULL::uuid document_id
                     FROM run_asset_promotion_subjects ps
                    WHERE %s='promotion_subject' AND ps.id=%s
                   UNION ALL
                   SELECT c.run_id,c.id,NULL::uuid,NULL::uuid
                     FROM search_candidates c
                    WHERE %s='search_candidate' AND c.id=%s
                   UNION ALL
                   SELECT ea.run_id,ea.candidate_id,NULL::uuid,NULL::uuid
                     FROM extraction_attempts ea
                    WHERE %s='extraction_attempt' AND ea.id=%s
                   UNION ALL
                   SELECT NULL::uuid,NULL::uuid,s.id,d.id
                     FROM asset_snapshots s
                     LEFT JOIN documents d ON d.snapshot_id=s.id
                    WHERE %s='source' AND s.source_id=%s
                   UNION ALL
                   SELECT NULL::uuid,NULL::uuid,s.id,d.id
                     FROM asset_snapshots s
                     LEFT JOIN documents d ON d.snapshot_id=s.id
                    WHERE %s='snapshot' AND s.id=%s
                   UNION ALL
                   SELECT NULL::uuid,NULL::uuid,d.snapshot_id,d.id
                     FROM documents d
                    WHERE %s='document' AND d.id=%s
                   UNION ALL
                   SELECT NULL::uuid,NULL::uuid,dd.snapshot_id,dd.document_id
                     FROM document_derivations dd
                    WHERE %s='derivation' AND dd.id=%s
                   UNION ALL
                   SELECT NULL::uuid,NULL::uuid,d.snapshot_id,c.document_id
                     FROM chunks c JOIN documents d ON d.id=c.document_id
                    WHERE %s='chunk' AND c.id=%s
               ), seed_attempts AS (
                   SELECT ea.id,ea.run_id,ea.candidate_id
                     FROM extraction_attempts ea
                    WHERE (%s='extraction_attempt' AND ea.id=%s)
                       OR EXISTS(
                           SELECT 1 FROM base b
                            WHERE b.candidate_id=ea.candidate_id
                              AND (b.run_id IS NULL OR b.run_id=ea.run_id)
                       )
                       OR EXISTS(
                           SELECT 1 FROM asset_snapshots s JOIN base b ON b.snapshot_id=s.id
                            WHERE s.extraction_attempt_id=ea.id
                       )
                       OR EXISTS(
                           SELECT 1 FROM documents d JOIN base b ON b.document_id=d.id
                            WHERE d.extraction_attempt_id=ea.id
                       )
               ), all_snapshots AS (
                   SELECT snapshot_id AS id FROM base WHERE snapshot_id IS NOT NULL
                   UNION
                   SELECT s.id FROM asset_snapshots s
                    WHERE s.extraction_attempt_id IN (SELECT id FROM seed_attempts)
               ), all_documents AS (
                   SELECT document_id AS id FROM base WHERE document_id IS NOT NULL
                   UNION
                   SELECT d.id FROM documents d
                    WHERE d.snapshot_id IN (SELECT id FROM all_snapshots)
                       OR d.extraction_attempt_id IN (SELECT id FROM seed_attempts)
               ), all_attempts AS (
                   SELECT * FROM seed_attempts
                   UNION
                   SELECT ea.id,ea.run_id,ea.candidate_id
                     FROM extraction_attempts ea
                    WHERE ea.id IN (
                        SELECT s.extraction_attempt_id FROM asset_snapshots s
                         WHERE s.id IN (SELECT id FROM all_snapshots)
                           AND s.extraction_attempt_id IS NOT NULL
                        UNION
                        SELECT d.extraction_attempt_id FROM documents d
                         WHERE d.id IN (SELECT id FROM all_documents)
                           AND d.extraction_attempt_id IS NOT NULL
                    )
               ), all_candidates AS (
                   SELECT candidate_id AS id,run_id FROM base WHERE candidate_id IS NOT NULL
                   UNION
                   SELECT candidate_id,run_id FROM all_attempts WHERE candidate_id IS NOT NULL
               ), all_runs AS (
                   SELECT run_id AS id FROM base WHERE run_id IS NOT NULL
                   UNION SELECT run_id FROM all_attempts WHERE run_id IS NOT NULL
               ), all_sources AS (
                   SELECT DISTINCT s.source_id AS id FROM asset_snapshots s
                    WHERE s.id IN (SELECT id FROM all_snapshots)
               ), all_derivations AS (
                   SELECT DISTINCT dd.id FROM document_derivations dd
                    WHERE dd.document_id IN (SELECT id FROM all_documents)
                       OR dd.snapshot_id IN (SELECT id FROM all_snapshots)
               ), all_chunks AS (
                   SELECT DISTINCT c.id FROM chunks c
                    WHERE c.document_id IN (SELECT id FROM all_documents)
               ), all_subjects AS (
                   SELECT DISTINCT ps.id
                     FROM run_asset_promotion_subjects ps
                    WHERE (%s='promotion_subject' AND ps.id=%s)
                       OR EXISTS(
                           SELECT 1 FROM all_candidates ac
                            WHERE ac.id=ps.candidate_id AND ac.run_id=ps.run_id
                       )
                       OR (
                           ps.snapshot_id IN (SELECT id FROM all_snapshots)
                           AND (
                               NOT EXISTS(SELECT 1 FROM all_runs)
                               OR ps.run_id IN (SELECT id FROM all_runs)
                           )
                       )
               )
               SELECT
                 ARRAY(SELECT DISTINCT id FROM all_runs ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_subjects ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_candidates ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_attempts ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_sources ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_snapshots ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_documents ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_derivations ORDER BY id),
                 ARRAY(SELECT DISTINCT id FROM all_chunks ORDER BY id)""",
            (
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
                kind,
                value,
            ),
        )
        row = cursor.fetchone()
    if row is None:
        raise CorpusIdentityResolutionError(f"could not crosswalk corpus identity: {value}")
    return CorpusIdentityResolution(
        identifier=value,
        identity_type=kind,
        run_ids=_uuid_tuple(row[0]),
        promotion_subject_ids=_uuid_tuple(row[1]),
        search_candidate_ids=_uuid_tuple(row[2]),
        extraction_attempt_ids=_uuid_tuple(row[3]),
        source_ids=_uuid_tuple(row[4]),
        snapshot_ids=_uuid_tuple(row[5]),
        document_ids=_uuid_tuple(row[6]),
        derivation_ids=_uuid_tuple(row[7]),
        chunk_ids=_uuid_tuple(row[8]),
    )


__all__ = [
    "CorpusIdentityResolution",
    "CorpusIdentityResolutionError",
    "resolve_corpus_identity",
]
