"""Fail-closed authority boundary for non-dry-run acquisition.

Authority invariants
--------------------
PostgreSQL is authoritative for workflow state, acquisition records,
provenance, corpus identities, and jobs. ``BLOB_ROOT`` is the immutable,
content-addressed payload store; payload bytes do not move into PostgreSQL.
Qdrant is a rebuildable projection, and Valkey is optional transient
coordination. Scratch paths and manifests are never acquisition authority.

Every acquisition entrypoint that can invoke Firecrawl or another network
transport must complete :func:`require_authoritative_acquisition` first (or
use :func:`execute_authoritative_acquisition`, which enforces that ordering).
A failed preflight is terminal for that attempted acquisition; callers must
not downgrade to diagnostic-only or otherwise non-persistent execution.

Secure, short-lived temporary files remain valid implementation details for
atomic writes and write probes. They are not persistent workflow state.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from .config import StoreConfig
from .postgres import connect

_T = TypeVar("_T")


class AcquisitionPreflightError(RuntimeError):
    """The authoritative acquisition contract is not satisfied."""


@dataclass(frozen=True)
class AuthoritativeAcquisitionContext:
    """Validated authority information safe to pass to an acquisition call."""

    database_url: str
    blob_root: Path
    schema_heads: frozenset[str]
    run_id: UUID | None
    dry_run: bool


def _expected_schema_heads() -> frozenset[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    return frozenset(script.get_heads())


def _probe_blob_root(blob_root: Path) -> None:
    """Verify atomic payload-store writes without creating persistent state."""
    try:
        blob_root.mkdir(parents=True, exist_ok=True)
        probe_path: Path | None = None
        renamed_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=blob_root,
                prefix=".acquisition-preflight-",
                delete=False,
            ) as probe:
                probe.write(b"authoritative-acquisition-preflight")
                probe.flush()
                os.fsync(probe.fileno())
                probe_path = Path(probe.name)
            renamed_path = probe_path.with_suffix(".verified")
            os.replace(probe_path, renamed_path)
            probe_path = None
        finally:
            for path in (probe_path, renamed_path):
                if path is not None:
                    path.unlink(missing_ok=True)
    except OSError as exc:
        raise AcquisitionPreflightError(
            f"BLOB_ROOT is not writable: {blob_root}: {exc}"
        ) from exc


def _normalize_run_id(run_id: UUID | str | None, *, dry_run: bool) -> UUID | None:
    if dry_run:
        if run_id is None:
            return None
    elif run_id is None:
        raise AcquisitionPreflightError(
            "a valid research run is required for non-dry-run acquisition"
        )

    try:
        return UUID(str(run_id)) if run_id is not None else None
    except (TypeError, ValueError, AttributeError) as exc:
        raise AcquisitionPreflightError(f"invalid research run ID: {run_id!r}") from exc


def require_authoritative_acquisition(
    *,
    run_id: UUID | str | None,
    dry_run: bool = False,
    config: StoreConfig | None = None,
    connect_factory: Callable[[str], object] = connect,
    expected_heads_factory: Callable[[], frozenset[str]] = _expected_schema_heads,
) -> AuthoritativeAcquisitionContext:
    """Require PostgreSQL, current schema, write authority, BLOB_ROOT, and run.

    This function performs no Firecrawl or other network invocation. Callers
    must invoke it before constructing or calling an acquisition transport.
    ``run_id`` may be omitted only for a true dry run.
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

                # A no-op write checks table-level UPDATE authority without
                # creating or mutating authoritative rows.
                cursor.execute("UPDATE research_runs SET id=id WHERE false")

                if normalized_run_id is not None:
                    cursor.execute(
                        "SELECT id FROM research_runs WHERE id=%s",
                        (normalized_run_id,),
                    )
                    if cursor.fetchone() is None:
                        raise AcquisitionPreflightError(
                            f"research run does not exist: {normalized_run_id}"
                        )
            connection.rollback()
    except AcquisitionPreflightError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AcquisitionPreflightError(
            f"authoritative PostgreSQL preflight failed: {exc}"
        ) from exc

    _probe_blob_root(resolved.blob_root)
    return AuthoritativeAcquisitionContext(
        database_url=resolved.database_url,
        blob_root=resolved.blob_root,
        schema_heads=expected_heads,
        run_id=normalized_run_id,
        dry_run=dry_run,
    )


def execute_authoritative_acquisition(
    operation: Callable[[AuthoritativeAcquisitionContext], _T],
    *,
    run_id: UUID | str | None,
    dry_run: bool = False,
    config: StoreConfig | None = None,
    connect_factory: Callable[[str], object] = connect,
    expected_heads_factory: Callable[[], frozenset[str]] = _expected_schema_heads,
) -> _T:
    """Execute ``operation`` only after the shared authority preflight passes."""
    context = require_authoritative_acquisition(
        run_id=run_id,
        dry_run=dry_run,
        config=config,
        connect_factory=connect_factory,
        expected_heads_factory=expected_heads_factory,
    )
    return operation(context)
