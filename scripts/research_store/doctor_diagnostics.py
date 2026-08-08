from __future__ import annotations

import errno
import os
import re
from datetime import datetime, timezone
from typing import Any

from .blob import ContentAddressedBlobStore
from .indexing import OpenAICompatibleEmbedder
from .retrieval import CohereCompatibleReranker

DOCTOR_SCHEMA_VERSION = "doctor-diagnostics-v1"
DOCTOR_DOMAINS = (
    "postgres_authority",
    "referenced_blob_integrity",
    "unreferenced_blob_inventory",
    "index_job_health",
    "qdrant_projection",
    "worker_health",
    "environment_connectivity",
)

_REMEDIATION = {
    "network_policy_denial": (
        "Allow the required endpoint or socket operation in the active sandbox, "
        "container, or host network policy, then rerun doctor."
    ),
    "server_unavailable": (
        "Verify the configured endpoint address and port, start the service, and "
        "confirm it is listening from this runtime."
    ),
    "credential_failure": (
        "Verify the configured URL, credentials, authentication settings, and "
        "service-side authorization without printing secret values."
    ),
    "network_namespace_denial": (
        "Verify DNS, routing, container/network-namespace attachment, and host "
        "reachability for the configured endpoint."
    ),
    "database_rejection": (
        "Inspect PostgreSQL role privileges, schema state, and the rejected query; "
        "apply the required grant or migration rather than bypassing authority."
    ),
    "query_runtime_failure": (
        "Inspect the named component and its logs for the runtime/query error, fix "
        "the underlying operation, and rerun doctor."
    ),
}


def _redact_diagnostic_detail(value: BaseException | str) -> str:
    """Return bounded diagnostic text with common credential forms removed."""
    detail = str(value)
    detail = re.sub(
        r"(?i)\bAuthorization\s*:\s*Bearer\s+[^\s,;]+",
        "Authorization: Bearer [REDACTED]",
        detail,
    )
    detail = re.sub(
        r"(?i)\bBearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        detail,
    )
    detail = re.sub(
        r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\s*([=:])\s*[^\s,;]+",
        r"\1\2[REDACTED]",
        detail,
    )
    detail = re.sub(
        r"(?i)(://[^:/@\s]+:)[^@\s/]+@",
        r"\1[REDACTED]@",
        detail,
    )
    return detail[:1000]


def _failure(reason_code: str, detail: BaseException | str) -> dict[str, str]:
    return {
        "status": "failure",
        "reason_code": reason_code,
        "detail": _redact_diagnostic_detail(detail),
        "remediation": _REMEDIATION[reason_code],
    }


def classify_connectivity_failure(
    exc: BaseException,
    *,
    component: str | None = None,
) -> dict[str, str]:
    """Classify one failed infrastructure operation without conflating domains."""
    message = str(exc).lower()
    error_number = getattr(exc, "errno", None)
    module_name = type(exc).__module__.lower()
    database_context = component in {
        "postgres_authority",
        "worker_health",
        "index_job_health",
    } or module_name.startswith(("psycopg", "sqlalchemy"))

    database_permission_tokens = (
        "permission denied for ",
        "insufficient privilege",
        "insufficient_privilege",
        "must be owner of ",
        "not authorized for relation",
    )
    if database_context and any(token in message for token in database_permission_tokens):
        return _failure("database_rejection", exc)

    if (
        isinstance(exc, ConnectionRefusedError)
        or error_number == errno.ECONNREFUSED
        or "connection refused" in message
        or "connect econnrefused" in message
        or re.search(r"\berrno\s*111\b", message)
        or "errno111" in message
    ):
        return _failure("server_unavailable", exc)

    if (
        error_number in {errno.ENETUNREACH, errno.EHOSTUNREACH}
        or "no route to host" in message
        or "network unreachable" in message
        or "name or service not known" in message
        or "temporary failure in name resolution" in message
        or re.search(r"\berrno\s*(101|113)\b", message)
        or "errno101" in message
        or "errno113" in message
    ):
        return _failure("network_namespace_denial", exc)

    if (
        isinstance(exc, PermissionError)
        or error_number in {errno.EPERM, errno.EACCES}
        or "operation not permitted" in message
    ):
        return _failure("network_policy_denial", exc)

    if any(
        token in message
        for token in (
            "password authentication failed",
            "authentication failed",
            "invalid password",
            "invalid credential",
            "credential",
            "unauthorized",
            "forbidden",
            "access denied",
            "api key",
            "api_key",
            "not configured",
        )
    ):
        return _failure("credential_failure", exc)

    if isinstance(exc, TimeoutError) or any(
        token in message for token in ("connection timed out", "timed out", "timeout")
    ):
        return _failure("server_unavailable", exc)

    if database_context or any(
        token in message
        for token in (
            "database",
            "postgres",
            "pg_",
            "psycopg",
            "sqlalchemy",
            "undefined table",
            "relation does not exist",
            "syntax error at or near",
        )
    ):
        return _failure("database_rejection", exc)

    return _failure("query_runtime_failure", exc)


def blob_health(config: Any) -> dict[str, Any]:
    """Inspect immutable referenced blobs and global disk inventory separately."""
    from . import cli

    store = ContentAddressedBlobStore(config.blob_root)
    with cli._db(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,content_sha256 FROM asset_snapshots")
        references = {digest: snapshot_id for snapshot_id, digest in cur.fetchall()}
    missing = [
        {"snapshot_id": references[digest], "sha256": digest}
        for digest in references
        if not store.verify(digest)
    ]
    disk_hashes = {
        path.name
        for path in config.blob_root.rglob("*")
        if path.is_file()
        and len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    }
    unreferenced = sorted(disk_hashes - references.keys())
    status = "pass" if not missing else "failure"
    return {
        "status": status,
        # Compatibility for the standalone verify-blobs result. Doctor itself
        # exposes the canonical per-domain ``status`` field.
        "integrity": status,
        "referenced": len(references),
        "missing_or_corrupt": missing,
        "unreferenced_inventory": unreferenced,
        "orphan_count": len(unreferenced),
    }


def _dependency_inconclusive(reason: str, remediation: str) -> dict[str, str]:
    return {
        "status": "inconclusive",
        "reason_code": reason,
        "remediation": remediation,
    }


def _qdrant_issue(qdrant: dict[str, Any], reason_code: str, remediation: str) -> None:
    qdrant["status"] = "failure"
    qdrant.setdefault("issues", []).append(
        {"reason_code": reason_code, "remediation": remediation}
    )


def _aggregate_component_status(components: dict[str, dict[str, Any]]) -> str:
    statuses = {component.get("status", "inconclusive") for component in components.values()}
    if "failure" in statuses:
        return "failure"
    if "inconclusive" in statuses:
        return "inconclusive"
    if "warning" in statuses:
        return "warning"
    return "pass"


def doctor(config: Any) -> tuple[dict[str, Any], bool]:
    """Return independent, machine-readable doctor domains for issue #220."""
    from . import cli

    checks: dict[str, Any] = {"schema_version": DOCTOR_SCHEMA_VERSION}
    failed = False
    schema: dict[str, Any] | None = None

    # PostgreSQL authority is its own domain. Worker health is deliberately
    # evaluated separately so a worker failure cannot erase authority status.
    try:
        schema = cli._schema_state(config)
        checks["schema"] = schema
        with cli._db(config) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status,count(*) FROM index_jobs GROUP BY status ORDER BY status"
            )
            checks["index_jobs"] = dict(cur.fetchall())
            if schema["at_head"]:
                cur.execute(
                    """SELECT count(*) FILTER(WHERE status IN ('partial','failed')),
                    min(started_at) FILTER(WHERE status='running') FROM ingestion_batches"""
                )
                bad, oldest_running = cur.fetchone()
                checks["ingestion_batches"] = {
                    "partial_or_failed": bad,
                    "oldest_running": oldest_running,
                }
        if schema["at_head"]:
            checks["postgres_authority"] = {"status": "pass"}
        else:
            checks["postgres_authority"] = {
                "status": "failure",
                "reason_code": "migration_required",
                "remediation": (
                    "Run research-db migrate against the authoritative PostgreSQL "
                    "database and verify the schema reaches the expected head."
                ),
            }
            failed = True
    except Exception as exc:  # noqa: BLE001
        checks["postgres_authority"] = classify_connectivity_failure(
            exc, component="postgres_authority"
        )
        failed = True

    if checks["postgres_authority"]["status"] != "pass":
        checks["worker_health"] = _dependency_inconclusive(
            "postgres_authority_unavailable",
            "Resolve postgres_authority before evaluating durable worker state.",
        )
    else:
        try:
            with cli._uow_factory(config)() as uow:
                worker = dict(uow.worker_status())
            workers = worker.get("workers", [])
            threshold = max(90, config.worker_poll_seconds * 4)
            heartbeat = workers[0].get("heartbeat_at") if workers else None
            age = (
                (datetime.now(timezone.utc) - heartbeat).total_seconds()
                if heartbeat is not None
                else None
            )
            worker["latest_heartbeat_age_seconds"] = (
                round(age, 3) if age is not None else None
            )
            worker["heartbeat_freshness_threshold_seconds"] = threshold
            worker["current_worker_available"] = (
                age is not None and age <= threshold
            ) or worker.get("active_leases", 0) > 0
            if worker.get("dead_jobs") or worker.get("stale_leases"):
                worker.update(
                    {
                        "status": "failure",
                        "reason_code": "durable_worker_state_unhealthy",
                        "remediation": (
                            "Resolve dead jobs or stale leases in PostgreSQL before "
                            "treating the worker domain as healthy."
                        ),
                    }
                )
                failed = True
            elif not worker["current_worker_available"]:
                worker.update(
                    {
                        "status": "failure",
                        "reason_code": "worker_unavailable",
                        "remediation": (
                            "Start or recover the persistent worker and confirm a fresh "
                            "heartbeat or live lease in PostgreSQL."
                        ),
                    }
                )
                failed = True
            else:
                worker["status"] = "pass"
            checks["worker_health"] = worker
        except Exception as exc:  # noqa: BLE001
            checks["worker_health"] = classify_connectivity_failure(
                exc, component="worker_health"
            )
            failed = True

    # Referenced integrity and global orphan inventory are independent domains.
    try:
        if not config.blob_root.is_dir():
            raise RuntimeError(f"blob root is not a directory: {config.blob_root}")
        if not os.access(config.blob_root, os.R_OK | os.X_OK):
            raise RuntimeError("blob root is not readable")
        health = cli._blob_health(config)
        referenced = {
            "status": health["status"],
            "referenced": health["referenced"],
            "missing_or_corrupt": health["missing_or_corrupt"],
        }
        if referenced["status"] == "failure":
            referenced.update(
                {
                    "reason_code": "referenced_blob_missing_or_corrupt",
                    "remediation": (
                        "Restore the referenced immutable blob bytes from the matching "
                        "backup boundary; do not infer or replace provenance."
                    ),
                }
            )
            failed = True
        checks["referenced_blob_integrity"] = referenced
        orphan_count = health["orphan_count"]
        inventory = {
            "status": "warning" if orphan_count else "pass",
            "orphan_count": orphan_count,
            "unreferenced": health["unreferenced_inventory"],
        }
        if orphan_count:
            inventory.update(
                {
                    "reason_code": "unreferenced_blobs_present",
                    "remediation": (
                        "Review the orphan inventory against backups and retention policy; "
                        "doctor never deletes orphan blobs automatically."
                    ),
                }
            )
        checks["unreferenced_blob_inventory"] = inventory
    except Exception as exc:  # noqa: BLE001
        checks["referenced_blob_integrity"] = {
            "status": "failure",
            "reason_code": "blob_root_unavailable",
            "detail": _redact_diagnostic_detail(exc),
            "remediation": (
                "Restore readable access to the configured BLOB_ROOT and rerun doctor."
            ),
        }
        checks["unreferenced_blob_inventory"] = _dependency_inconclusive(
            "blob_inventory_unavailable",
            "Restore readable BLOB_ROOT access before interpreting orphan inventory.",
        )
        failed = True

    # Qdrant is checked only as a rebuildable projection of PostgreSQL chunks.
    try:
        aliases = cli._qdrant(config).list_aliases()
        active = aliases.get(config.qdrant_alias)
        qdrant: dict[str, Any] = {
            "status": "pass" if active else "inconclusive",
            "alias": config.qdrant_alias,
            "collection": active,
            "issues": [],
        }
        if not active:
            qdrant.update(
                {
                    "reason_code": "active_projection_absent",
                    "remediation": (
                        "Build and validate a PostgreSQL-derived Qdrant collection before "
                        "activating the stable alias."
                    ),
                }
            )
        elif not (schema and schema.get("at_head")):
            qdrant["schema"] = cli._qdrant(config, active).inspect_schema()
            qdrant.update(
                {
                    "status": "inconclusive",
                    "reason_code": "postgres_authority_unavailable",
                    "remediation": (
                        "Resolve PostgreSQL schema authority before comparing projection "
                        "membership."
                    ),
                }
            )
        else:
            rows = [
                row
                for row in cli._index_rows(config)
                if row["physical_collection"] == active
            ]
            if not rows:
                raise RuntimeError("active alias is not backed by an index definition")
            row = rows[0]
            qdrant["query_embedding_compatible"] = (
                row["fingerprint"] == config.embedding_fingerprint
            )
            if not qdrant["query_embedding_compatible"]:
                _qdrant_issue(
                    qdrant,
                    "embedding_fingerprint_mismatch",
                    "Rebuild the projection from PostgreSQL with the active embedding fingerprint.",
                )
                failed = True
            qdrant["schema"] = cli._qdrant(
                config, active, row["dimension"], row["distance_metric"]
            ).inspect_schema()
            if not qdrant["schema"]["compatible"]:
                _qdrant_issue(
                    qdrant,
                    "qdrant_schema_incompatible",
                    "Rebuild the projection with the configured dimension and distance metric.",
                )
                failed = True
            point_ids: set[str] = set()
            offset = None
            active_index = cli._qdrant(
                config, active, row["dimension"], row["distance_metric"]
            )
            while True:
                page = active_index.point_ids(
                    offset, filters=cli._derivation_filter(config)
                )
                point_ids.update(str(item["id"]) for item in page.get("points", []))
                offset = page.get("next_page_offset")
                if not offset:
                    break
            chunk_ids = {str(value) for value in cli._active_chunk_ids(config)}
            qdrant["coverage"] = {
                "missing": len(chunk_ids - point_ids),
                "orphaned": len(point_ids - chunk_ids),
            }
            if point_ids != chunk_ids:
                _qdrant_issue(
                    qdrant,
                    "projection_membership_mismatch",
                    "Reconcile or rebuild Qdrant from authoritative PostgreSQL chunks.",
                )
                failed = True
            elif qdrant["status"] != "failure":
                # Coverage success must never erase an earlier compatibility failure.
                qdrant["status"] = "pass"
        checks["qdrant_projection"] = qdrant
    except Exception as exc:  # noqa: BLE001
        checks["qdrant_projection"] = classify_connectivity_failure(
            exc, component="qdrant_projection"
        )
        failed = True

    try:
        if schema and schema.get("at_head"):
            reconcile = cli._index_reconcile(config, repair=False)
            index_health = {
                "status": "pass" if reconcile["ok"] else "failure",
                "total_active_chunks": reconcile["total_active_chunks"],
                "definitions": len(reconcile["definitions"]),
                "discrepancies": reconcile["discrepancies"],
            }
            if not reconcile["ok"]:
                index_health.update(
                    {
                        "reason_code": "index_reconciliation_failed",
                        "remediation": (
                            "Run index-reconcile and repair durable manifest/job discrepancies "
                            "before relying on projection health."
                        ),
                    }
                )
                failed = True
            checks["index_job_health"] = index_health
        else:
            checks["index_job_health"] = _dependency_inconclusive(
                "postgres_authority_unavailable",
                "Resolve postgres_authority before evaluating durable index jobs.",
            )
    except Exception as exc:  # noqa: BLE001
        checks["index_job_health"] = classify_connectivity_failure(
            exc, component="index_job_health"
        )
        failed = True

    components: dict[str, dict[str, Any]] = {}
    try:
        import redis

        if bool(redis.Redis.from_url(config.valkey_url).ping()):
            components["valkey"] = {"status": "pass"}
        else:
            components["valkey"] = {
                "status": "failure",
                "reason_code": "valkey_ping_failed",
                "remediation": "Start Valkey and verify the configured VALKEY_URL is reachable.",
            }
    except Exception as exc:  # noqa: BLE001
        components["valkey"] = classify_connectivity_failure(exc, component="valkey")

    for name, endpoint in (
        ("embedding", config.embedding_url),
        ("reranker", config.reranker_url),
    ):
        try:
            if not endpoint:
                raise RuntimeError(f"{name.upper()}_URL is not configured")
            if name == "embedding":
                vector = OpenAICompatibleEmbedder(
                    endpoint,
                    config.embedding_model,
                    config.embedding_api_key,
                    config.embedding_dimension,
                )("research-store-doctor")
                components[name] = {"status": "pass", "dimension": len(vector)}
            else:
                ranked = CohereCompatibleReranker(
                    endpoint, config.reranker_model, config.reranker_api_key
                )(
                    "research database",
                    [
                        {"candidate_id": "relevant", "excerpt": "research database"},
                        {"candidate_id": "other", "excerpt": "yellow bananas"},
                    ],
                )
                if not ranked or ranked[0]["candidate_id"] != "relevant":
                    raise RuntimeError("unexpected reranker ordering")
                components[name] = {"status": "pass"}
        except Exception as exc:  # noqa: BLE001
            components[name] = classify_connectivity_failure(exc, component=name)

    environment_status = _aggregate_component_status(components)
    checks["environment_connectivity"] = {
        "status": environment_status,
        "components": components,
    }
    if environment_status == "failure":
        failed = True

    checks["configuration"] = {
        "embedding_fingerprint": config.embedding_fingerprint,
        "physical_collection": config.physical_collection,
        "normalization_version": config.normalization_version,
        "parser_version": config.parser_version,
        "chunker_version": config.chunker_version,
    }
    return checks, failed


def format_human(checks: dict[str, Any]) -> str:
    """Render the same independent domains without changing their status meaning."""
    lines = [f"Research store doctor ({checks.get('schema_version', DOCTOR_SCHEMA_VERSION)})"]
    for domain in DOCTOR_DOMAINS:
        record = checks.get(domain) or {
            "status": "inconclusive",
            "reason_code": "domain_missing",
        }
        line = f"{domain}: {record.get('status', 'inconclusive')}"
        if record.get("reason_code"):
            line += f" [{record['reason_code']}]"
        lines.append(line)
        if record.get("remediation"):
            lines.append(f"  remediation: {record['remediation']}")
        if domain == "unreferenced_blob_inventory":
            lines.append(f"  orphan_count: {record.get('orphan_count', 'unknown')}")
    return "\n".join(lines)
