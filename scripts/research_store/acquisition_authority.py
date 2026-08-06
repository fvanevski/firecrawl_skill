"""Fail-closed readiness checks for PostgreSQL-authoritative acquisition.

Authority invariants
--------------------
PostgreSQL is authoritative for workflow state, acquisition records,
invocations, provenance, corpus identities, and jobs. ``BLOB_ROOT`` remains the
immutable, content-addressed payload store; payload bytes do not move into
PostgreSQL. Qdrant remains a rebuildable projection, and Valkey remains
optional transient coordination. Local paths and manifests are never runtime
authority.

Every entrypoint that may invoke Firecrawl or another network transport must
call :func:`require_authoritative_acquisition` before constructing or invoking
that transport. A failed preflight is terminal for that attempted acquisition;
callers must not downgrade to a non-persistent execution mode.

A successful preflight is only a pre-network readiness snapshot. It is not an
acquisition result and must never be reported as success. The provider response
must still be committed through an authoritative acquisition service using an
idempotency key. That service must revalidate the captured run lifecycle
revision, persist the immutable payload in ``BLOB_ROOT``, commit PostgreSQL,
and return authoritative identifiers only after commit.

Secure, short-lived temporary files remain valid implementation details for
atomic writes and write probes. The probe below fsyncs both the file and its
containing directory and removes its files and any newly created empty
``BLOB_ROOT`` directories before returning.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from .config import StoreConfig
from .postgres import connect
from .run_service import TERMINAL_STATES


class AcquisitionPreflightError(RuntimeError):
    """The authoritative acquisition readiness contract is not satisfied."""


# Direct provider acquisition is valid only after an explicit lifecycle command
# has prepared the run. Wrapper calls must never move a run into this state as a
# side effect of beginning a provider invocation.
ACQUISITION_ENTRY_STATES = frozenset({"acquiring"})

ACQUISITION_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "research_runs": frozenset({"SELECT", "UPDATE"}),
    "search_responses": frozenset({"SELECT", "INSERT"}),
    "search_candidates": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "candidate_occurrences": frozenset({"SELECT", "INSERT"}),
    "research_events": frozenset({"SELECT", "INSERT"}),
}


@dataclass(frozen=True)
class AuthoritativeAcquisitionContext:
    """Validated pre-network authority snapshot for an acquisition service.

    ``lifecycle_revision`` is deliberately captured so the authoritative
    persistence transaction can reject a stale or newly terminal run. The raw
    database URL is excluded from ``repr`` to avoid leaking credentials.
    """

    database_url: str = field(repr=False)
    blob_root: Path
    schema_heads: frozenset[str]
    run_id: UUID | None
    run_state: str | None
    lifecycle_revision: int | None
    dry_run: bool


def _expected_schema_heads() -> frozenset[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    return frozenset(script.get_heads())


def _created_directories(path: Path) -> list[Path]:
    """Return missing directories from leaf to the first existing ancestor."""
    created: list[Path] = []
    current = path
    while not current.exists():
        created.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return created


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _probe_blob_root(blob_root: Path) -> None:
    """Verify atomic durable payload writes without leaving probe state."""
    created_dirs = _created_directories(blob_root)
    probe_path: Path | None = None
    renamed_path: Path | None = None
    try:
        blob_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=blob_root,
            prefix=".acquisition-preflight-",
            delete=False,
        ) as probe:
            probe.write(b"authoritative-acquisition-preflight")
            probe.flush()
            os.fsync(probe.fileno())
            probe_path = Path(probe.name)

        renamed_path = probe_path.with_name(f"{probe_path.name}.verified")
        os.replace(probe_path, renamed_path)
        probe_path = None
        _fsync_directory(blob_root)

        renamed_path.unlink()
        renamed_path = None
        _fsync_directory(blob_root)
    except OSError as exc:
        raise AcquisitionPreflightError(
            f"BLOB_ROOT is not durably writable: {blob_root}: {exc}"
        ) from exc
    finally:
        for path in (probe_path, renamed_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        for directory in created_dirs:
            try:
                directory.rmdir()
            except OSError:
                break


def _normalize_run_id(run_id: UUID | str | None, *, dry_run: bool) -> UUID | None:
    if run_id is None:
        if dry_run:
            return None
        raise AcquisitionPreflightError(
            "a valid research run is required for non-dry-run acquisition"
        )

    try:
        return UUID(str(run_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AcquisitionPreflightError(f"invalid research run ID: {run_id!r}") from exc


def _require_acquisition_privileges(cursor: object) -> None:
    missing: list[str] = []
    for table, privileges in ACQUISITION_TABLE_PRIVILEGES.items():
        for privilege in sorted(privileges):
            cursor.execute(
                "SELECT has_table_privilege(current_user, %s, %s)",
                (table, privilege),
            )
            row = cursor.fetchone()
            if not row or row[0] is not True:
                missing.append(f"{table}:{privilege}")
    if missing:
        raise AcquisitionPreflightError(
            "authoritative PostgreSQL role lacks acquisition privileges: "
            + ", ".join(missing)
        )


def require_authoritative_acquisition(
    *,
    run_id: UUID | str | None,
    dry_run: bool = False,
    config: StoreConfig | None = None,
    connect_factory: Callable[[str], object] = connect,
    expected_heads_factory: Callable[[], frozenset[str]] = _expected_schema_heads,
) -> AuthoritativeAcquisitionContext:
    """Validate authority readiness before any provider or network execution.

    The returned context is a snapshot, not proof of an acquisition commit.
    Callers must pass ``run_id`` and ``lifecycle_revision`` into the subsequent
    authoritative persistence transaction and use compare-and-swap semantics
    before returning success.
    """
    resolved = config or StoreConfig.from_env()
    try:
        resolved.require_database()
    except (RuntimeError, ValueError) as exc:
        raise AcquisitionPreflightError(str(exc)) from exc

    normalized_run_id = _normalize_run_id(run_id, dry_run=dry_run)
    expected_heads = frozenset(expected_heads_factory())
    if not expected_heads:
        raise AcquisitionPreflightError("Alembic has no configured schema head")

    run_state: str | None = None
    lifecycle_revision: int | None = None
    try:
        with connect_factory(resolved.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                current_heads = frozenset(row[0] for row in cursor.fetchall())
                if current_heads != expected_heads:
                    raise AcquisitionPreflightError(
                        "PostgreSQL schema is not at Alembic head: "
                        f"current={sorted(current_heads)!r}, "
                        f"expected={sorted(expected_heads)!r}"
                    )

                cursor.execute("SHOW transaction_read_only")
                row = cursor.fetchone()
                read_only = not row or str(row[0]).strip().lower() not in {
                    "off",
                    "false",
                    "0",
                }
                if read_only:
                    raise AcquisitionPreflightError(
                        "authoritative PostgreSQL connection is read-only"
                    )

                _require_acquisition_privileges(cursor)

                if normalized_run_id is not None:
                    cursor.execute(
                        """SELECT id, state, lifecycle_revision
                        FROM research_runs WHERE id=%s FOR SHARE""",
                        (normalized_run_id,),
                    )
                    run_row = cursor.fetchone()
                    if run_row is None:
                        raise AcquisitionPreflightError(
                            f"research run does not exist: {normalized_run_id}"
                        )
                    run_state = str(run_row[1])
                    lifecycle_revision = int(run_row[2])
                    if run_state in TERMINAL_STATES:
                        raise AcquisitionPreflightError(
                            f"research run is terminal ({run_state}); reopen it "
                            "before acquisition"
                        )
                    if run_state not in ACQUISITION_ENTRY_STATES:
                        raise AcquisitionPreflightError(
                            "research run state is not acquisition-eligible: "
                            f"{run_state}; explicitly prepare the run before "
                            "direct acquisition"
                        )
            connection.rollback()
    except AcquisitionPreflightError:
        raise
    except Exception as exc:
        raise AcquisitionPreflightError(
            f"authoritative PostgreSQL preflight failed: {exc}"
        ) from exc

    _probe_blob_root(resolved.blob_root)
    return AuthoritativeAcquisitionContext(
        database_url=resolved.database_url,
        blob_root=resolved.blob_root,
        schema_heads=expected_heads,
        run_id=normalized_run_id,
        run_state=run_state,
        lifecycle_revision=lifecycle_revision,
        dry_run=dry_run,
    )
