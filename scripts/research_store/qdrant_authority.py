"""Cross-store Qdrant activation authority and release-safety checks.

PostgreSQL owns index lifecycle state. Qdrant owns only the rebuildable vector
projection. This module centralizes the small amount of logic that must compare
those two authorities so doctor, reconciliation, and release preflight do not
implement subtly different alias rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config import StoreConfig
from .postgres import connect
from .qdrant import QdrantIndex


def evaluate_required_alias_state(
    *,
    aliases: Mapping[str, str],
    required_alias_name: str,
    active_definitions: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate the exact PostgreSQL-active-definition/required-alias contract.

    A healthy projection has exactly one PostgreSQL-active definition and the
    configured required alias targets that definition's exact physical
    collection. An unrelated alias targeting the same collection never
    substitutes for the configured alias.
    """
    normalized = [dict(definition) for definition in active_definitions]
    active_ids = [str(definition["id"]) for definition in normalized]
    actual_target = aliases.get(required_alias_name)

    base: dict[str, Any] = {
        "postgres_active_definition": active_ids[0] if len(active_ids) == 1 else None,
        "postgres_active_definitions": active_ids,
        "required_alias_name": required_alias_name,
        "actual_required_alias_target": actual_target,
        "expected_active_collection": None,
        "dimension": None,
        "distance_metric": None,
    }

    if not normalized:
        return {
            **base,
            "status": "no_active_definition",
            "expected": "exactly one PostgreSQL-active index definition",
            "actual": (
                f"no PostgreSQL-active definition; {required_alias_name} -> "
                f"{actual_target or 'absent'}"
            ),
        }

    if len(normalized) != 1:
        return {
            **base,
            "status": "multiple_active_definitions",
            "expected": "exactly one PostgreSQL-active index definition",
            "actual": f"{len(normalized)} active definitions: {', '.join(active_ids)}",
        }

    definition = normalized[0]
    expected_collection = str(definition["physical_collection"])
    base.update(
        {
            "expected_active_collection": expected_collection,
            "dimension": int(definition["dimension"]),
            "distance_metric": str(definition["distance_metric"]),
        }
    )

    if actual_target is None:
        return {
            **base,
            "status": "missing_required_alias",
            "expected": f"{required_alias_name} -> {expected_collection}",
            "actual": f"{required_alias_name} is absent",
        }

    if actual_target != expected_collection:
        return {
            **base,
            "status": "wrong_required_alias_target",
            "expected": f"{required_alias_name} -> {expected_collection}",
            "actual": f"{required_alias_name} -> {actual_target}",
        }

    return {
        **base,
        "status": "healthy",
        "expected": f"{required_alias_name} -> {expected_collection}",
        "actual": f"{required_alias_name} -> {actual_target}",
    }


def read_required_alias_state(config: StoreConfig) -> dict[str, Any]:
    """Read the exact cross-store activation state without mutating either store."""
    aliases = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.qdrant_alias,
        config.embedding_dimension,
    ).list_aliases()
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,physical_collection,dimension,distance_metric
                 FROM index_definitions
                WHERE lifecycle_status='active'
                ORDER BY activated_at DESC NULLS LAST,created_at DESC,id"""
        )
        active_definitions = [
            {
                "id": row[0],
                "physical_collection": row[1],
                "dimension": row[2],
                "distance_metric": row[3],
            }
            for row in cursor.fetchall()
        ]
    return evaluate_required_alias_state(
        aliases=aliases,
        required_alias_name=config.qdrant_alias,
        active_definitions=active_definitions,
    )


def capture_configured_projection_state(
    config: StoreConfig,
    *,
    sample_limit: int = 32,
) -> dict[str, Any]:
    """Capture a read-only release-safety snapshot of the configured projection.

    The configured embedding fingerprint must agree with the PostgreSQL-active
    definition and the required alias. The snapshot includes the current point
    count and a bounded set of existing point IDs so a later probe can prove it
    did not delete or repoint the active projection.
    """
    alias_state = read_required_alias_state(config)
    if alias_state["status"] != "healthy":
        raise RuntimeError(
            "active Qdrant projection is not authoritative: "
            f"{alias_state['status']} ({alias_state['actual']}; "
            f"expected {alias_state['expected']})"
        )
    target = alias_state["actual_required_alias_target"]
    if target != config.physical_collection:
        raise RuntimeError(
            f"configured embedding fingerprint expects {config.physical_collection}, "
            f"but PostgreSQL/Qdrant authority resolves to {target}"
        )

    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        target,
        alias_state["dimension"],
        alias_state["distance_metric"],
    )
    schema = index.inspect_schema()
    if not schema.get("exists") or not schema.get("compatible"):
        raise RuntimeError(f"active collection schema is incompatible: {schema!r}")
    points_count = schema.get("points_count")
    if not isinstance(points_count, int) or points_count < 0:
        raise RuntimeError(
            f"active collection did not expose a valid points_count: {points_count!r}"
        )

    page = index.point_ids(limit=max(1, sample_limit), filters=None)
    sample_ids = tuple(str(item["id"]) for item in page.get("points", []))
    return {
        "required_alias_name": config.qdrant_alias,
        "target_collection": target,
        "postgres_active_definition": alias_state["postgres_active_definition"],
        "dimension": alias_state["dimension"],
        "distance_metric": alias_state["distance_metric"],
        "schema_actual": schema.get("actual"),
        "schema_expected": schema.get("expected"),
        "points_count": points_count,
        "sample_ids": sample_ids,
    }


def require_configured_projection_preserved(
    config: StoreConfig,
    before: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed if a probe repointed, shrank, or deleted baseline projection data."""
    after = capture_configured_projection_state(config)
    identity_fields = (
        "required_alias_name",
        "target_collection",
        "postgres_active_definition",
        "dimension",
        "distance_metric",
        "schema_actual",
        "schema_expected",
    )
    changed = [field for field in identity_fields if after.get(field) != before.get(field)]
    if changed:
        raise RuntimeError(
            "active Qdrant projection identity changed during probe: "
            + ", ".join(
                f"{field}={before.get(field)!r}->{after.get(field)!r}"
                for field in changed
            )
        )

    before_count = before.get("points_count")
    after_count = after.get("points_count")
    if not isinstance(before_count, int) or not isinstance(after_count, int):
        raise TypeError("projection point-count evidence is unavailable")
    if after_count < before_count:
        raise RuntimeError(
            f"active Qdrant projection lost points during probe: "
            f"{before_count} -> {after_count}"
        )

    sample_ids = tuple(str(value) for value in before.get("sample_ids", ()))
    if sample_ids:
        index = QdrantIndex(
            config.qdrant_url,
            config.qdrant_api_key,
            str(after["target_collection"]),
            int(after["dimension"]),
            str(after["distance_metric"]),
        )
        returned = {
            str(point.get("id"))
            for point in index.retrieve(sample_ids, with_payload=False)
        }
        missing = sorted(set(sample_ids) - returned)
        if missing:
            raise RuntimeError(
                "active Qdrant projection lost baseline points during probe: "
                + ", ".join(missing[:10])
            )

    return after
