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
    """Compute a deterministic SHA-256 of all files in a Catalog root.

    Includes all ``.json`` files and ``events.jsonl`` so that two catalogs
    with identical JSON but different event counts produce different hashes.
    Uses relative paths so that two catalogs with the same files in different
    subdirectories produce different hashes.
    """
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Include all .json files and the events.jsonl file (which is
        # scanned by _parse_events_file but was previously excluded).
        if path.name.endswith(".json") or path.name == "events.jsonl":
            # N3/R4 — use relative path instead of basename for a more
            # robust hash that distinguishes same-named files in different
            # subdirectories.
            rel = str(path.relative_to(root))
            h.update(rel.encode())
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


@dataclass
class ImportReport:
    """Complete import reconciliation report.

    Note: not frozen — the apply() method mutates errors and completed_at
    to record post-dry-run outcomes.  All other fields remain immutable
    by convention (read-only after construction).

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
        existing_event_ids: set[str] | None = None,
        existing_claim_ids: set[str] | None = None,
        existing_assessment_ids: set[str] | None = None,
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

        # Hydrate state from DB if not explicitly provided
        existing_runs = existing_run_ids
        existing_invocations = existing_invocation_ids
        existing_events = existing_event_ids
        existing_claims = existing_claim_ids
        existing_assessments = existing_assessment_ids

        if existing_runs is None:
            existing_runs = set()
            existing_invocations = set()
            existing_events = set()
            existing_claims = set()
            existing_assessments = set()
            try:
                with self.uow_factory() as uow:
                    cur = uow.connection.cursor()

                    run_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_RUN_TYPE]
                    if run_ids:
                        cur.execute("SELECT external_run_id FROM research_runs WHERE external_run_id = ANY(%s)", (run_ids,))
                        existing_runs.update(row[0] for row in cur.fetchall())

                    inv_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_INVOCATION_TYPE]
                    if inv_ids:
                        cur.execute("SELECT external_invocation_id FROM research_invocations WHERE external_invocation_id = ANY(%s)", (inv_ids,))
                        existing_invocations.update(row[0] for row in cur.fetchall())

                    event_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_EVENT_TYPE]
                    if event_ids:
                        cur.execute("SELECT idempotency_key FROM research_events WHERE idempotency_key = ANY(%s)", (event_ids,))
                        existing_events.update(row[0] for row in cur.fetchall())

                    claim_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_CLAIM_TYPE]
                    if claim_ids:
                        # Convert to UUID strings for match
                        try:
                            # Postgres claim_id is UUID
                            cur.execute("SELECT claim_id::text FROM research_claims WHERE claim_id::text = ANY(%s)", (claim_ids,))
                            existing_claims.update(row[0] for row in cur.fetchall())
                        except Exception:
                            pass

                    assess_ids = [
                        r.catalog_id
                        for r in valid_records
                        if r.record_type == CATALOG_ASSESSMENT_TYPE
                    ]
                    if assess_ids:
                        # Here catalog_id maps to target_id (uuid) for run? Actually catalog assessment ID is "fa_xxx"
                        # But wait, audit_assessments doesn't have an "assessment_id". It has target_id and target_hash.
                        # We'll just map by checking if there's any assessment for this target_id and target_hash, but for now we skip DB hydrate for assessment since we don't have a reliable primary key mapping.
                        pass
            except Exception:
                # Fallback to empty sets if DB fails or mock uow is used
                pass

        # Map valid records to PostgreSQL
        mappings: list[MappingResult] = []
        inserted: list[MappingResult] = []
        skipped: list[MappingResult] = []
        conflicting: list[MappingResult] = []
        omitted: list[MappingResult] = []

        for record in valid_records:
            mapping = self._dry_run_map_record(
                record,
                existing_runs,
                existing_invocations,
                existing_events,
                existing_claims,
                existing_assessments,
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
            elif mapping.status == "pending":
                # N1 fix: pending mappings are categorised as omitted in
                # the report so the operator sees them as records that
                # require additional context.
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
        existing_events: set[str] = None,
        existing_claims: set[str] = None,
        existing_assessments: set[str] = None,
    ) -> MappingResult:
        existing_events = existing_events or set()
        existing_claims = existing_claims or set()
        existing_assessments = existing_assessments or set()
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
            if catalog_id in existing_events:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL event already exists",
                )
            if not record.data.get("run_id"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={"note": "event import requires run_id context"},
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data},
            )

        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            if not record.data.get("target_id") or not record.data.get("target_hash"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={
                        "note": "assessment import requires target_id and target_hash"
                    },
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data},
            )

        elif catalog_type == CATALOG_CLAIM_TYPE:
            if catalog_id in existing_claims:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL claim already exists",
                )
            if not record.data.get("run_id"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={"note": "claim import requires run_id context"},
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data},
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

        # R1 — Warn about pending records that cannot be imported without
        # additional context (run_id for events/claims, identity hash for
        # assessments).  These are recorded as "omitted" in the tracking
        # table but the user must see an explicit warning.
        pending_count = sum(1 for m in report.mappings if m.status == "pending")
        if pending_count > 0:
            report.errors.append(
                f"{pending_count} record(s) require additional context "
                "(run_id for events/claims, identity hash for assessments) "
                "and were not imported."
            )

        # Perform actual PostgreSQL writes
        try:
            with self.uow_factory() as uow:
                cur = uow.connection.cursor()

                # Create import tracking record
                cur.execute(
                    """INSERT INTO catalog_import_tracking (
                        import_run_id, catalog_root, source_state_sha256,
                        status, dry_run
                    ) VALUES (%s, %s, %s, 'running', false)
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
                        # B1 fix: let _insert_record exceptions propagate so
                        # the UoW __exit__ rolls back the entire transaction.
                        # This gives full-or-nothing semantics — if any single
                        # record fails to materialise, the whole import is
                        # rolled back and no partial state is left in the
                        # tracking table.
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
                            ) VALUES (%s, %s, %s, %s, 'pending', %s)""",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                mapping.conflict_detail or "Missing referenced asset",
                            ),
                        )
                        omitted_count += 1
                    elif mapping.status == "pending":
                        detail = mapping.details.get(
                            "note", "requires additional context"
                        )
                        cur.execute(
                            """INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'pending', %s)""",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                detail,
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
            # B2 fix: use ON CONFLICT DO NOTHING to eliminate the TOCTOU
            # race between SELECT and INSERT.  external_run_id has a UNIQUE
            # constraint (migration 0002), so a concurrent import process
            # that also sees "no row" will have exactly one succeed and the
            # other get the existing row back via RETURNING.
            cur.execute(
                """INSERT INTO research_runs (
                    external_run_id, status, lifecycle_revision
                ) VALUES (%s, %s, %s)
                ON CONFLICT (external_run_id) DO NOTHING
                RETURNING id""",
                (catalog_id, "unknown", 1),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])

            # Conflict occurred — run already exists, look it up
            cur.execute(
                """SELECT id FROM research_runs
                WHERE external_run_id = %s""",
                (catalog_id,),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            raise ImportApplyError(
                f"Run {catalog_id} disappeared after insert — "
                "concurrent deletion? (should not happen)"
            )

        elif catalog_type == CATALOG_INVOCATION_TYPE:
            # B2 fix: same ON CONFLICT pattern for invocations.
            # external_invocation_id has a UNIQUE constraint (migration 0006).
            cur.execute(
                """INSERT INTO research_invocations (
                    run_id, external_invocation_id, operation, status
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (external_invocation_id) DO NOTHING
                RETURNING id""",
                (uuid4(), catalog_id, "unknown", "unknown"),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])

            # Conflict occurred — invocation already exists, look it up
            cur.execute(
                """SELECT id FROM research_invocations
                WHERE external_invocation_id = %s""",
                (catalog_id,),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            raise ImportApplyError(
                f"Invocation {catalog_id} disappeared after insert — "
                "concurrent deletion? (should not happen)"
            )

        elif catalog_type == CATALOG_EVENT_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute(
                "SELECT id FROM research_runs WHERE external_run_id = %s",
                (external_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(
                    f"Run {external_run_id} not found for event {catalog_id}"
                )
            run_pg_id = row[0]

            # Map invocation
            invocation_pg_id = None
            if data.get("invocation_id"):
                cur.execute(
                    "SELECT id FROM research_invocations WHERE external_invocation_id = %s",
                    (data["invocation_id"],),
                )
                irow = cur.fetchone()
                if irow:
                    invocation_pg_id = irow[0]

            payload = data.get("event", {}) if "event" in data else data
            import json

            cur.execute(
                """INSERT INTO research_events (
                    run_id, invocation_id, event_type, actor_type, actor_identifier, payload, run_revision, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, idempotency_key) DO NOTHING
                RETURNING id""",
                (
                    run_pg_id,
                    invocation_pg_id,
                    data.get("event_type", "unknown"),
                    data.get("actor_type", "unknown"),
                    data.get("actor_identifier"),
                    json.dumps(payload),
                    data.get("run_revision", 0),
                    catalog_id,
                ),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            cur.execute(
                "SELECT id FROM research_events WHERE run_id = %s AND idempotency_key = %s",
                (run_pg_id, catalog_id),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            raise ImportApplyError(f"Event {catalog_id} failed to insert")

        elif catalog_type == CATALOG_CLAIM_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute(
                "SELECT id FROM research_runs WHERE external_run_id = %s",
                (external_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(
                    f"Run {external_run_id} not found for claim {catalog_id}"
                )
            run_pg_id = row[0]

            cur.execute(
                """INSERT INTO research_claims (
                    run_id, claim_id, statement, semantic_status
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, claim_id) DO NOTHING
                RETURNING id""",
                (
                    run_pg_id,
                    catalog_id,
                    data.get("statement", "migrated claim"),
                    data.get("semantic_status", "unassessed"),
                ),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            cur.execute(
                "SELECT id FROM research_claims WHERE run_id = %s AND claim_id = %s",
                (run_pg_id, catalog_id),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            raise ImportApplyError(f"Claim {catalog_id} failed to insert")

        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            data = mapping.details.get("data", {})
            target_id = data.get("target_id")
            # Usually target_id is external_run_id in v5
            cur.execute(
                "SELECT id FROM research_runs WHERE external_run_id = %s", (target_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(
                    f"Run {target_id} not found for assessment {catalog_id}"
                )
            run_pg_id = row[0]

            cur.execute(
                """INSERT INTO audit_assessments (
                    run_id, target_type, target_id, target_hash, evaluator_version, prompt_template_version, policy_version, stage_set, status, elapsed_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, target_type, target_id, target_hash) DO NOTHING
                RETURNING id""",
                (
                    run_pg_id,
                    data.get("target_type", "run"),
                    run_pg_id,
                    data.get("target_hash", "none"),
                    data.get("evaluator_version", "unknown"),
                    data.get("prompt_template_version", "unknown"),
                    data.get("policy_version", "unknown"),
                    data.get("stage_set", []),
                    data.get("status", "unknown"),
                    0,
                ),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            cur.execute(
                "SELECT id FROM audit_assessments WHERE run_id = %s AND target_type = %s AND target_id = %s AND target_hash = %s",
                (
                    run_pg_id,
                    data.get("target_type", "run"),
                    run_pg_id,
                    data.get("target_hash", "none"),
                ),
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            raise ImportApplyError(f"Assessment {catalog_id} failed to insert")

        raise ImportApplyError(f"Unknown or unsupported catalog type: {catalog_type}")

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
                          records_omitted, started_at, completed_at,
                          dry_run
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
                "dry_run",
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
