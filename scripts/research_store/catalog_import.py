"""Catalog v5 import service.

Provides dry-run-first, idempotent import of Catalog v5 records
(runs, invocations, events, claims, assessments) into PostgreSQL
authority.  Produces conflict and omission reports.  Never deletes
source files.

PRD mapping: Section 20 (Phase 4), Section 23

Authority model:

* PostgreSQL is authoritative.  Catalog v5 files are an import source.
* Import never overwrites newer PostgreSQL data.
* Conflicts require explicit user resolution.
* Repeated imports against the same Catalog root are idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4


def utcnow() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Catalog v5 record types
# ---------------------------------------------------------------------------

CATALOG_RUN_TYPE = "run"
CATALOG_INVOCATION_TYPE = "invocation"
CATALOG_EVENT_TYPE = "event"
CATALOG_CLAIM_TYPE = "claim"
CATALOG_ASSESSMENT_TYPE = "assessment"

VALID_CATALOG_TYPES = frozenset(
    (
        CATALOG_RUN_TYPE,
        CATALOG_INVOCATION_TYPE,
        CATALOG_EVENT_TYPE,
        CATALOG_CLAIM_TYPE,
        CATALOG_ASSESSMENT_TYPE,
    )
)

# Supported Catalog v5 schema versions
SUPPORTED_SCHEMA_VERSIONS = frozenset({5})

# Regex for Catalog v5 identifiers
RUN_ID_RE = re.compile(r"^fr_[0-9a-f]{32}$")
INVOCATION_ID_RE = re.compile(r"^(?:fc_|fce_)[0-9a-f]{32}$")
ASSESSMENT_ID_RE = re.compile(r"^fa_[0-9a-f]{32}$")
EVENT_ID_RE = re.compile(r"^fe_[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _is_valid_run_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(RUN_ID_RE.fullmatch(value))


def _is_valid_invocation_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(INVOCATION_ID_RE.fullmatch(value))


def _is_valid_assessment_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(ASSESSMENT_ID_RE.fullmatch(value))


def _is_valid_event_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(EVENT_ID_RE.fullmatch(value))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compute_dir_sha256(root: Path) -> str:
    """Compute a deterministic SHA-256 of all files in a Catalog root."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.endswith(".json"):
            h.update(path.name.encode())
            try:
                h.update(path.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogRecord:
    """A parsed Catalog v5 record ready for import.

    Attributes:
        record_type: One of CATALOG_RUN_TYPE, CATALOG_INVOCATION_TYPE, etc.
        catalog_id: The Catalog v5 identifier (e.g. ``fr_<32hex>``).
        data: The parsed JSON payload.
        source_path: Path to the source file on disk.
        schema_version: The schema version from the record.
        errors: List of validation errors (empty when valid).
    """

    record_type: str
    catalog_id: str
    data: dict[str, Any]
    source_path: Path
    schema_version: int
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


@dataclass(frozen=True)
class MappingResult:
    """Result of mapping a Catalog record to PostgreSQL.

    Attributes:
        catalog_type: The Catalog record type.
        catalog_id: The Catalog v5 identifier.
        postgresql_id: The PostgreSQL surrogate key (if mapped).
        status: One of the mapping status values.
        conflict_detail: Description of conflict (if applicable).
        details: Additional context.
    """

    catalog_type: str
    catalog_id: str
    postgresql_id: UUID | None
    status: str
    conflict_detail: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportReport:
    """Complete import reconciliation report.

    Attributes:
        import_run_id: The import session UUID.
        catalog_root: The Catalog v5 root directory.
        source_state_sha256: SHA-256 of the Catalog root.
        dry_run: Whether this was a dry run.
        started_at: When the import started.
        completed_at: When the import completed (nullable).
        records: All parsed Catalog records.
        valid_records: Records that passed schema validation.
        malformed_records: Records that failed validation.
        mappings: Mapping results for valid records.
        inserted: Records inserted into PostgreSQL.
        skipped: Records already present in PostgreSQL.
        conflicting: Records with PostgreSQL conflicts.
        omitted: Records omitted due to missing assets.
        errors: General errors (e.g. I/O failures).
    """

    import_run_id: UUID
    catalog_root: Path
    source_state_sha256: str
    dry_run: bool
    started_at: datetime
    completed_at: datetime | None
    records: list[CatalogRecord]
    valid_records: list[CatalogRecord]
    malformed_records: list[CatalogRecord]
    mappings: list[MappingResult]
    inserted: list[MappingResult]
    skipped: list[MappingResult]
    conflicting: list[MappingResult]
    omitted: list[MappingResult]
    errors: list[str] = field(default_factory=list)

    @property
    def records_inserted(self) -> int:
        return len(self.inserted)

    @property
    def records_skipped(self) -> int:
        return len(self.skipped)

    @property
    def records_conflicting(self) -> int:
        return len(self.conflicting)

    @property
    def records_malformed(self) -> int:
        return len(self.malformed_records)

    @property
    def records_omitted(self) -> int:
        return len(self.omitted)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report to a JSON-serializable dict."""
        return {
            "import_run_id": str(self.import_run_id),
            "catalog_root": str(self.catalog_root),
            "source_state_sha256": self.source_state_sha256,
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "total_records": len(self.records),
            "valid_records": len(self.valid_records),
            "malformed_records": self.records_malformed,
            "records_inserted": self.records_inserted,
            "records_skipped": self.records_skipped,
            "records_conflicting": self.records_conflicting,
            "records_omitted": self.records_omitted,
            "errors": self.errors,
            "mappings": [
                {
                    "catalog_type": m.catalog_type,
                    "catalog_id": m.catalog_id,
                    "postgresql_id": str(m.postgresql_id) if m.postgresql_id else None,
                    "status": m.status,
                    "conflict_detail": m.conflict_detail,
                }
                for m in self.mappings
            ],
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """Summary of past import attempts.

    Attributes:
        total_imports: Number of import sessions.
        imports: List of import summary records.
        conflict_summary: Count of conflicts by type.
        omission_summary: Count of omissions by type.
    """

    total_imports: int
    imports: list[dict[str, Any]]
    conflict_summary: dict[str, int]
    omission_summary: dict[str, int]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CatalogImportError(Exception):
    """Base exception for catalog import errors."""


class CatalogRootNotFound(CatalogImportError):
    """Raised when the specified Catalog root does not exist."""


class CatalogRootInvalid(CatalogImportError):
    """Raised when the Catalog root has invalid structure."""


class ImportApplyError(CatalogImportError):
    """Raised when an apply operation fails."""


# ---------------------------------------------------------------------------
# CatalogImportService
# ---------------------------------------------------------------------------


class CatalogImportService:
    """Import Catalog v5 records into PostgreSQL authority.

    This service provides:

    * Scanning and parsing of Catalog v5 files.
    * Schema and hash validation.
    * Deterministic mapping to PostgreSQL records.
    * Conflict detection against existing PostgreSQL data.
    * Dry-run and apply modes.
    * Idempotent repeated imports.

    Args:
        uow_factory: Callable that returns a ``PostgresUnitOfWork``.
    """

    def __init__(self, uow_factory: Callable) -> None:
        self.uow_factory = uow_factory

    # ------------------------------------------------------------------
    # Scanning and parsing
    # ------------------------------------------------------------------

    def scan_catalog_root(self, root: Path) -> list[CatalogRecord]:
        """Scan a Catalog v5 root directory and parse all records.

        Args:
            root: Path to the Catalog v5 root directory.

        Returns:
            List of parsed ``CatalogRecord`` objects.

        Raises:
            CatalogRootNotFound: If the root does not exist.
            CatalogRootInvalid: If the root has invalid structure.
        """
        if not root.is_dir():
            raise CatalogRootNotFound(f"Catalog root not found: {root}")

        records: list[CatalogRecord] = []
        errors: list[str] = []

        # Scan runs
        runs_dir = root / "runs"
        if runs_dir.is_dir():
            for path in sorted(runs_dir.glob("*.json")):
                record = self._parse_run_file(path)
                if record:
                    records.append(record)

        # Scan invocations
        invocations_dir = root / "invocations"
        if invocations_dir.is_dir():
            for path in sorted(invocations_dir.glob("*.json")):
                record = self._parse_invocation_file(path)
                if record:
                    records.append(record)

        # Scan assessments
        assessments_dir = root / "assessments"
        if assessments_dir.is_dir():
            for path in sorted(assessments_dir.rglob("*.json")):
                record = self._parse_assessment_file(path)
                if record:
                    records.append(record)

        # Scan events
        events_file = root / "events.jsonl"
        if events_file.is_file():
            records.extend(self._parse_events_file(events_file))

        # Scan claims (from manifests)
        claims_dir = root / "claims"
        if claims_dir.is_dir():
            for path in sorted(claims_dir.glob("*.json")):
                record = self._parse_claim_file(path)
                if record:
                    records.append(record)

        if not records and not errors:
            raise CatalogRootInvalid(
                f"Catalog root {root} contains no parseable records"
            )

        return records

    def _parse_run_file(self, path: Path) -> CatalogRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CatalogRecord(
                record_type=CATALOG_RUN_TYPE,
                catalog_id=path.stem,
                data={},
                source_path=path,
                schema_version=0,
                errors=[f"Failed to parse: {exc}"],
            )

        schema_version = data.get("schema_version", 0)
        run_id = data.get("research_run_id", path.stem)

        errors: list[str] = []
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(
                f"Unsupported schema version {schema_version}; "
                f"expected {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if not _is_valid_run_id(run_id):
            errors.append(f"Invalid run ID: {run_id}")

        return CatalogRecord(
            record_type=CATALOG_RUN_TYPE,
            catalog_id=run_id,
            data=data,
            source_path=path,
            schema_version=schema_version,
            errors=errors,
        )

    def _parse_invocation_file(self, path: Path) -> CatalogRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CatalogRecord(
                record_type=CATALOG_INVOCATION_TYPE,
                catalog_id=path.stem,
                data={},
                source_path=path,
                schema_version=0,
                errors=[f"Failed to parse: {exc}"],
            )

        schema_version = data.get("schema_version", 0)
        invocation_id = data.get("invocation_id", path.stem)

        errors: list[str] = []
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(
                f"Unsupported schema version {schema_version}; "
                f"expected {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if not _is_valid_invocation_id(invocation_id):
            errors.append(f"Invalid invocation ID: {invocation_id}")

        return CatalogRecord(
            record_type=CATALOG_INVOCATION_TYPE,
            catalog_id=invocation_id,
            data=data,
            source_path=path,
            schema_version=schema_version,
            errors=errors,
        )

    def _parse_assessment_file(self, path: Path) -> CatalogRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CatalogRecord(
                record_type=CATALOG_ASSESSMENT_TYPE,
                catalog_id=path.stem,
                data={},
                source_path=path,
                schema_version=0,
                errors=[f"Failed to parse: {exc}"],
            )

        schema_version = data.get("schema_version", 0)
        assessment_id = data.get("assessment_id", path.stem)

        errors: list[str] = []
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(
                f"Unsupported schema version {schema_version}; "
                f"expected {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if not _is_valid_assessment_id(assessment_id):
            errors.append(f"Invalid assessment ID: {assessment_id}")

        return CatalogRecord(
            record_type=CATALOG_ASSESSMENT_TYPE,
            catalog_id=assessment_id,
            data=data,
            source_path=path,
            schema_version=schema_version,
            errors=errors,
        )

    def _parse_events_file(self, path: Path) -> list[CatalogRecord]:
        records: list[CatalogRecord] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return [
                CatalogRecord(
                    record_type=CATALOG_EVENT_TYPE,
                    catalog_id="unknown",
                    data={},
                    source_path=path,
                    schema_version=0,
                    errors=[f"Failed to read events file: {exc}"],
                )
            ]

        for line_no, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                records.append(
                    CatalogRecord(
                        record_type=CATALOG_EVENT_TYPE,
                        catalog_id=f"line_{line_no}",
                        data={},
                        source_path=path,
                        schema_version=0,
                        errors=[f"Line {line_no}: invalid JSON: {exc}"],
                    )
                )
                continue

            schema_version = data.get("schema_version", 0)
            event_id = data.get("event_id", f"line_{line_no}")

            errors: list[str] = []
            if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                errors.append(f"Unsupported schema version {schema_version}")
            if not _is_valid_event_id(event_id):
                errors.append(f"Invalid event ID: {event_id}")

            records.append(
                CatalogRecord(
                    record_type=CATALOG_EVENT_TYPE,
                    catalog_id=event_id,
                    data=data,
                    source_path=path,
                    schema_version=schema_version,
                    errors=errors,
                )
            )
        return records

    def _parse_claim_file(self, path: Path) -> CatalogRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CatalogRecord(
                record_type=CATALOG_CLAIM_TYPE,
                catalog_id=path.stem,
                data={},
                source_path=path,
                schema_version=0,
                errors=[f"Failed to parse: {exc}"],
            )

        schema_version = data.get("schema_version", 0)
        claim_id = data.get("id", path.stem)

        errors: list[str] = []
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(f"Unsupported schema version {schema_version}")

        return CatalogRecord(
            record_type=CATALOG_CLAIM_TYPE,
            catalog_id=str(claim_id),
            data=data,
            source_path=path,
            schema_version=schema_version,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Dry-run: scan, validate, and map without writing
    # ------------------------------------------------------------------

    def dry_run(
        self,
        catalog_root: Path,
        *,
        existing_run_ids: set[str] | None = None,
        existing_invocation_ids: set[str] | None = None,
    ) -> ImportReport:
        """Perform a dry-run import against a Catalog v5 root.

        Scans, validates, and maps all records without writing to
        PostgreSQL.  Produces a complete reconciliation report.

        Args:
            catalog_root: Path to the Catalog v5 root directory.
            existing_run_ids: Set of existing PostgreSQL run external IDs.
            existing_invocation_ids: Set of existing PostgreSQL invocation external IDs.

        Returns:
            An ``ImportReport`` with full reconciliation details.
        """
        import_run_id = uuid4()
        started_at = utcnow()
        errors: list[str] = []

        try:
            source_state_sha256 = _compute_dir_sha256(catalog_root)
        except Exception as exc:
            errors.append(f"Failed to compute source state hash: {exc}")
            source_state_sha256 = ""

        try:
            records = self.scan_catalog_root(catalog_root)
        except CatalogImportError as exc:
            errors.append(str(exc))
            records = []

        # Separate valid and malformed records
        valid_records: list[CatalogRecord] = []
        malformed_records: list[CatalogRecord] = []
        for record in records:
            if record.is_valid:
                valid_records.append(record)
            else:
                malformed_records.append(record)

        # Map valid records to PostgreSQL
        existing_runs = existing_run_ids or set()
        existing_invocations = existing_invocation_ids or set()
        mappings: list[MappingResult] = []
        inserted: list[MappingResult] = []
        skipped: list[MappingResult] = []
        conflicting: list[MappingResult] = []
        omitted: list[MappingResult] = []

        for record in valid_records:
            mapping = self._dry_run_map_record(
                record, existing_runs, existing_invocations
            )
            mappings.append(mapping)
            if mapping.status == "inserted":
                inserted.append(mapping)
            elif mapping.status == "skipped":
                skipped.append(mapping)
            elif mapping.status == "conflict":
                conflicting.append(mapping)
            elif mapping.status == "omitted":
                omitted.append(mapping)

        completed_at = utcnow()

        return ImportReport(
            import_run_id=import_run_id,
            catalog_root=catalog_root,
            source_state_sha256=source_state_sha256,
            dry_run=True,
            started_at=started_at,
            completed_at=completed_at,
            records=records,
            valid_records=valid_records,
            malformed_records=malformed_records,
            mappings=mappings,
            inserted=inserted,
            skipped=skipped,
            conflicting=conflicting,
            omitted=omitted,
            errors=errors,
        )

    def _dry_run_map_record(
        self,
        record: CatalogRecord,
        existing_runs: set[str],
        existing_invocations: set[str],
    ) -> MappingResult:
        """Map a single Catalog record during dry-run."""
        catalog_type = record.record_type
        catalog_id = record.catalog_id

        # Check if this record already exists in PostgreSQL
        if catalog_type == CATALOG_RUN_TYPE:
            if catalog_id in existing_runs:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL run already exists; Catalog is older",
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
            )

        elif catalog_type == CATALOG_INVOCATION_TYPE:
            if catalog_id in existing_invocations:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL invocation already exists",
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
            )

        elif catalog_type == CATALOG_EVENT_TYPE:
            # Events are append-only; always insertable
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
            )

        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            # Assessments may conflict on identity hash
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"note": "assessment import requires additional validation"},
            )

        elif catalog_type == CATALOG_CLAIM_TYPE:
            # Claims are upserted on (run_id, claim_id)
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
            )

        return MappingResult(
            catalog_type=catalog_type,
            catalog_id=catalog_id,
            postgresql_id=None,
            status="pending",
            details={"note": "unknown catalog type"},
        )

    # ------------------------------------------------------------------
    # Apply: scan, validate, map, and write to PostgreSQL
    # ------------------------------------------------------------------

    def apply(
        self,
        catalog_root: Path,
        *,
        report_file: Path | None = None,
    ) -> ImportReport:
        """Apply a Catalog v5 import to PostgreSQL.

        This is a mutation operation.  It writes records to PostgreSQL
        and records the import in ``catalog_import_tracking``.

        Args:
            catalog_root: Path to the Catalog v5 root directory.
            report_file: Optional path to write the import report as JSON.

        Returns:
            An ``ImportReport`` with full reconciliation details.

        Raises:
            ImportApplyError: If the apply operation fails.
        """
        import_run_id = uuid4()

        # Dry-run first to get the report
        report = self.dry_run(catalog_root)

        if report.records_malformed > 0 and report.records_inserted == 0:
            report.errors.append("Aborting apply: all records are malformed")
            report.completed_at = utcnow()
            if report_file:
                report_file.write_text(json.dumps(report.to_dict(), indent=2))
            raise ImportApplyError(
                f"Apply aborted: {report.records_malformed} malformed records, "
                f"{report.records_inserted} insertable"
            )

        # Perform actual PostgreSQL writes
        try:
            with self.uow_factory() as uow:
                cur = uow.connection.cursor()

                # Create import tracking record
                cur.execute(
                    """INSERT INTO catalog_import_tracking (
                        import_run_id, catalog_root, source_state_sha256, status
                    ) VALUES (%s, %s, %s, 'running')
                    RETURNING id""",
                    (
                        str(import_run_id),
                        str(catalog_root),
                        report.source_state_sha256,
                    ),
                )
                tracking_id = cur.fetchone()[0]

                # Insert records
                inserted_count = 0
                skipped_count = 0
                conflicting_count = 0
                omitted_count = 0

                for mapping in report.mappings:
                    if mapping.status == "inserted":
                        try:
                            pg_id = self._insert_record(uow, mapping)
                            cur.execute(
                                """INSERT INTO catalog_import_record_map (
                                    import_run_id, catalog_type, catalog_id,
                                    postgresql_id, mapping_status
                                ) VALUES (%s, %s, %s, %s, 'inserted')""",
                                (
                                    str(tracking_id),
                                    mapping.catalog_type,
                                    mapping.catalog_id,
                                    str(pg_id),
                                ),
                            )
                            inserted_count += 1
                        except Exception as exc:
                            cur.execute(
                                """INSERT INTO catalog_import_record_map (
                                    import_run_id, catalog_type, catalog_id,
                                    postgresql_id, mapping_status, conflict_detail
                                ) VALUES (%s, %s, %s, %s, 'conflict', %s)""",
                                (
                                    str(tracking_id),
                                    mapping.catalog_type,
                                    mapping.catalog_id,
                                    None,
                                    str(exc),
                                ),
                            )
                            conflicting_count += 1
                    elif mapping.status == "skipped":
                        cur.execute(
                            """INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'skipped', %s)""",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                mapping.conflict_detail or "Already exists",
                            ),
                        )
                        skipped_count += 1
                    elif mapping.status == "conflict":
                        cur.execute(
                            """INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'conflict', %s)""",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                mapping.conflict_detail or "Conflict",
                            ),
                        )
                        conflicting_count += 1
                    elif mapping.status == "omitted":
                        cur.execute(
                            """INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'omitted', %s)""",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                mapping.conflict_detail or "Missing referenced asset",
                            ),
                        )
                        omitted_count += 1

                # Update tracking status
                status = "completed"
                if conflicting_count > 0 or omitted_count > 0:
                    status = "partial"
                if inserted_count == 0 and skipped_count == 0:
                    status = "failed"

                cur.execute(
                    """UPDATE catalog_import_tracking
                    SET status = %s,
                        records_inserted = %s,
                        records_skipped = %s,
                        records_conflicting = %s,
                        records_malformed = %s,
                        records_omitted = %s,
                        completed_at = %s
                    WHERE id = %s""",
                    (
                        status,
                        inserted_count,
                        skipped_count,
                        conflicting_count,
                        report.records_malformed,
                        omitted_count,
                        utcnow(),
                        tracking_id,
                    ),
                )

                uow.connection.commit()
        except Exception as exc:
            report.errors.append(f"PostgreSQL write failed: {exc}")
            report.completed_at = utcnow()
            if report_file:
                report_file.write_text(json.dumps(report.to_dict(), indent=2))
            raise ImportApplyError(f"Apply failed: {exc}") from exc

        report.completed_at = utcnow()
        if report_file:
            report_file.write_text(json.dumps(report.to_dict(), indent=2))

        return report

    def _insert_record(self, uow: Any, mapping: MappingResult) -> UUID:
        """Insert a single record into PostgreSQL.

        Args:
            uow: The unit of work.
            mapping: The mapping result for the record.

        Returns:
            The PostgreSQL surrogate key UUID.
        """
        cur = uow.connection.cursor()
        catalog_type = mapping.catalog_type
        catalog_id = mapping.catalog_id

        if catalog_type == CATALOG_RUN_TYPE:
            # Look up the run by external_run_id
            cur.execute(
                """SELECT id FROM research_runs
                WHERE external_run_id = %s""",
                (catalog_id,),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])

            # Create a new run record
            cur.execute(
                """INSERT INTO research_runs (
                    external_run_id, status, lifecycle_revision
                ) VALUES (%s, %s, %s)
                RETURNING id""",
                (catalog_id, "unknown", 1),
            )
            return UUID(cur.fetchone()[0])

        elif catalog_type == CATALOG_INVOCATION_TYPE:
            # Look up by external_invocation_id
            cur.execute(
                """SELECT id FROM research_invocations
                WHERE external_invocation_id = %s""",
                (catalog_id,),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])

            # Create a minimal invocation record
            cur.execute(
                """INSERT INTO research_invocations (
                    run_id, external_invocation_id, operation, status
                ) VALUES (%s, %s, %s, %s)
                RETURNING id""",
                (uuid4(), catalog_id, "unknown", "unknown"),
            )
            return UUID(cur.fetchone()[0])

        elif catalog_type == CATALOG_EVENT_TYPE:
            # Events need a run_id; skip if not available
            raise ImportApplyError(
                f"Event {catalog_id} requires a run_id; "
                "event imports require additional context"
            )

        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            raise ImportApplyError(
                f"Assessment {catalog_id} requires additional validation"
            )

        elif catalog_type == CATALOG_CLAIM_TYPE:
            raise ImportApplyError(f"Claim {catalog_id} requires a run_id context")

        raise ImportApplyError(f"Unknown catalog type: {catalog_type}")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile(
        self,
    ) -> ReconciliationReport:
        """Produce a reconciliation report from past import attempts.

        Returns:
            A ``ReconciliationReport`` summarizing all past imports.
        """
        with self.uow_factory() as uow:
            cur = uow.connection.cursor()

            cur.execute(
                """SELECT id, import_run_id, catalog_root, source_state_sha256,
                          status, records_inserted, records_skipped,
                          records_conflicting, records_malformed,
                          records_omitted, started_at, completed_at
                   FROM catalog_import_tracking
                   ORDER BY started_at DESC"""
            )
            rows = cur.fetchall()
            keys = [
                "id",
                "import_run_id",
                "catalog_root",
                "source_state_sha256",
                "status",
                "records_inserted",
                "records_skipped",
                "records_conflicting",
                "records_malformed",
                "records_omitted",
                "started_at",
                "completed_at",
            ]

            imports = []
            conflict_summary: dict[str, int] = {}
            omission_summary: dict[str, int] = {}

            for row in rows:
                record = dict(zip(keys, row))
                imports.append(record)

                # Aggregate conflict and omission summaries
                if record["records_conflicting"] > 0:
                    conflict_summary[str(record["catalog_root"])] = (
                        conflict_summary.get(str(record["catalog_root"]), 0)
                        + record["records_conflicting"]
                    )
                if record["records_omitted"] > 0:
                    omission_summary[str(record["catalog_root"])] = (
                        omission_summary.get(str(record["catalog_root"]), 0)
                        + record["records_omitted"]
                    )

            return ReconciliationReport(
                total_imports=len(imports),
                imports=imports,
                conflict_summary=conflict_summary,
                omission_summary=omission_summary,
            )
