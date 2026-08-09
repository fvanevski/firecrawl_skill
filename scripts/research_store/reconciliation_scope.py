"""PostgreSQL scope loading for Qdrant reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .config import StoreConfig
from .postgres import connect


class ReconciliationError(RuntimeError):
    """Raised when authoritative reconciliation scope cannot be established."""


@dataclass(frozen=True)
class ReconciliationScope:
    scope: str
    run_id: UUID | None
    external_run_id: str | None
    checkpoint_id: UUID | None
    seal_id: UUID | None
    seal_status: str | None
    fingerprint: str
    definition: dict[str, Any]
    expected_ids: tuple[UUID, ...]
    membership_sha256: str | None = None
    asset_membership_sha256: str | None = None


def _definition_from_row(row) -> dict[str, Any]:
    keys = (
        "id",
        "fingerprint",
        "physical_collection",
        "model_name",
        "model_revision",
        "dimension",
        "distance_metric",
        "normalization",
        "instruction_template_hash",
        "lifecycle_status",
    )
    result = dict(zip(keys, row))
    result["id"] = str(result["id"])
    return result


def _load_definition(cursor, fingerprint: str) -> dict[str, Any]:
    cursor.execute(
        """SELECT id,fingerprint,physical_collection,model_name,model_revision,
                  dimension,distance_metric,normalization,
                  instruction_template_hash,lifecycle_status
             FROM index_definitions WHERE fingerprint=%s
             ORDER BY created_at,id""",
        (fingerprint,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ReconciliationError(
            (
                f"checkpoint fingerprint {fingerprint} resolves to "
                f"{len(rows)} index definitions"
            )
        )
    return _definition_from_row(rows[0])


def resolve_run_id(config: StoreConfig, identifier: str | UUID) -> UUID:
    """Resolve a UUID or external run ID without imposing lifecycle state."""
    try:
        candidate = UUID(str(identifier))
    except ValueError:
        candidate = None
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        if candidate is not None:
            cursor.execute("SELECT id FROM research_runs WHERE id=%s", (candidate,))
        else:
            cursor.execute(
                "SELECT id FROM research_runs WHERE external_run_id=%s",
                (str(identifier),),
            )
        row = cursor.fetchone()
    if row is None:
        raise ReconciliationError(f"research run not found: {identifier}")
    return UUID(str(row[0]))


def _load_run_scope(config: StoreConfig, identifier: str | UUID) -> ReconciliationScope:
    run_id = resolve_run_id(config, identifier)
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT external_run_id FROM research_runs WHERE id=%s", (run_id,)
        )
        external_run_id = cursor.fetchone()[0]
        cursor.execute(
            """SELECT id,fingerprint,entity_ids,expected_membership_sha256,
                      expected_count,asset_membership_seal_id,
                      asset_membership_sha256,asset_expected_chunk_count,status
                 FROM indexing_checkpoints
                WHERE run_id=%s AND status='completed'
                ORDER BY completed_at DESC NULLS LAST,created_at DESC,id DESC
                LIMIT 1""",
            (run_id,),
        )
        checkpoint = cursor.fetchone()
        if checkpoint is None:
            raise ReconciliationError(
                "run has no completed indexing checkpoint; historical membership "
                "will not be inferred"
            )
        (
            checkpoint_id,
            fingerprint,
            entity_ids,
            expected_membership_sha256,
            expected_count,
            seal_id,
            checkpoint_asset_sha,
            asset_expected_chunk_count,
            _checkpoint_status,
        ) = checkpoint
        if seal_id is None or checkpoint_asset_sha is None:
            raise ReconciliationError(
                "completed checkpoint predates authoritative asset-membership binding; "
                "historical membership will not be fabricated"
            )
        entity_ids_tuple = tuple(UUID(str(value)) for value in (entity_ids or ()))
        if not entity_ids_tuple or len(entity_ids_tuple) != int(expected_count):
            raise ReconciliationError(
                "completed checkpoint entity_ids do not match its expected_count"
            )

        cursor.execute(
            """SELECT status,membership_sha256,expected_chunk_count
                 FROM run_asset_membership_seals
                WHERE id=%s AND run_id=%s""",
            (seal_id, run_id),
        )
        seal = cursor.fetchone()
        if seal is None:
            raise ReconciliationError("checkpoint references a missing membership seal")
        seal_status, seal_sha, seal_chunk_count = seal
        if str(seal_sha) != str(checkpoint_asset_sha):
            raise ReconciliationError(
                "checkpoint asset-membership SHA does not match its seal"
            )
        if int(seal_chunk_count) != int(asset_expected_chunk_count):
            raise ReconciliationError(
                "checkpoint asset chunk count does not match its seal"
            )
        if int(seal_chunk_count) != len(entity_ids_tuple):
            raise ReconciliationError(
                "sealed asset chunk count does not match checkpoint membership"
            )

        cursor.execute(
            """SELECT chunk_ids FROM run_asset_membership_members
                WHERE seal_id=%s AND run_id=%s ORDER BY ordinal""",
            (seal_id, run_id),
        )
        member_ids = sorted(
            {
                UUID(str(chunk_id))
                for row in cursor.fetchall()
                for chunk_id in (row[0] or ())
            },
            key=str,
        )
        if tuple(member_ids) != tuple(sorted(entity_ids_tuple, key=str)):
            raise ReconciliationError(
                "checkpoint entity_ids differ from the persisted membership-seal "
                "members"
            )
        definition = _load_definition(cursor, str(fingerprint))

    return ReconciliationScope(
        scope="run",
        run_id=run_id,
        external_run_id=external_run_id,
        checkpoint_id=UUID(str(checkpoint_id)),
        seal_id=UUID(str(seal_id)),
        seal_status=str(seal_status),
        fingerprint=str(fingerprint),
        definition=definition,
        expected_ids=tuple(sorted(entity_ids_tuple, key=str)),
        membership_sha256=str(expected_membership_sha256),
        asset_membership_sha256=str(checkpoint_asset_sha),
    )


def _load_projection_scope(config: StoreConfig) -> ReconciliationScope:
    """Load current embedding-definition scope for compatibility diagnostics.

    This is intentionally distinct from run reconciliation.  It makes no claim
    about historical run membership and exists for ``doctor``/legacy internal
    callers that need projection-wide health.
    """
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        definition = _load_definition(cursor, config.embedding_fingerprint)
        cursor.execute(
            """SELECT DISTINCT m.chunk_id
                 FROM embedding_manifests m
                 JOIN chunks c ON c.id=m.chunk_id
                WHERE m.index_definition_id=%s
                ORDER BY m.chunk_id""",
            (UUID(definition["id"]),),
        )
        expected = tuple(UUID(str(row[0])) for row in cursor.fetchall())
    return ReconciliationScope(
        scope="projection",
        run_id=None,
        external_run_id=None,
        checkpoint_id=None,
        seal_id=None,
        seal_status=None,
        fingerprint=definition["fingerprint"],
        definition=definition,
        expected_ids=expected,
    )


def _load_postgres_projection_state(
    config: StoreConfig, scope: ReconciliationScope
) -> dict[str, Any]:
    definition_id = UUID(scope.definition["id"])
    expected = list(scope.expected_ids)
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        if expected:
            cursor.execute(
                """SELECT m.id,m.chunk_id,m.index_status
                     FROM embedding_manifests m
                    WHERE m.index_definition_id=%s AND m.chunk_id=ANY(%s)""",
                (definition_id, expected),
            )
        else:
            cursor.execute(
                """SELECT m.id,m.chunk_id,m.index_status
                     FROM embedding_manifests m WHERE false"""
            )
        manifest_rows = cursor.fetchall()
        manifests = {
            str(row[1]): {"id": str(row[0]), "status": str(row[2])}
            for row in manifest_rows
        }
        manifest_ids = [UUID(row["id"]) for row in manifests.values()]
        jobs: dict[str, list[str]] = {}
        if manifest_ids:
            cursor.execute(
                """SELECT m.chunk_id,j.status
                     FROM index_jobs j
                     JOIN embedding_manifests m ON m.id=j.manifest_id
                    WHERE j.operation='upsert' AND j.manifest_id=ANY(%s)""",
                (manifest_ids,),
            )
            for chunk_id, status in cursor.fetchall():
                jobs.setdefault(str(chunk_id), []).append(str(status))

        cursor.execute(
            """SELECT DISTINCT m.chunk_id
                 FROM embedding_manifests m
                 JOIN chunks c ON c.id=m.chunk_id
                WHERE m.index_definition_id=%s""",
            (definition_id,),
        )
        definition_ids = {str(row[0]) for row in cursor.fetchall()}

        if expected:
            cursor.execute(
                """SELECT c.id,d.snapshot_id,d.id AS document_id,s.id AS source_id,
                          s.registered_domain,d.published_at
                     FROM chunks c
                     JOIN documents d ON d.id=c.document_id
                     JOIN asset_snapshots a ON a.id=d.snapshot_id
                     JOIN sources s ON s.id=a.source_id
                    WHERE c.id=ANY(%s)""",
                (expected,),
            )
        else:
            cursor.execute(
                """SELECT c.id,d.snapshot_id,d.id,s.id,s.registered_domain,
                          d.published_at
                     FROM chunks c JOIN documents d ON false
                     JOIN asset_snapshots a ON false JOIN sources s ON false"""
            )
        payloads: dict[str, dict[str, Any]] = {}
        for row in cursor.fetchall():
            payloads[str(row[0])] = {
                "snapshot_id": str(row[1]) if row[1] is not None else None,
                "document_id": str(row[2]) if row[2] is not None else None,
                "source_id": str(row[3]) if row[3] is not None else None,
                "domain": row[4],
                "published_at": row[5].isoformat() if row[5] is not None else None,
            }
    return {
        "manifests": manifests,
        "jobs": jobs,
        "definition_ids": definition_ids,
        "payloads": payloads,
    }
