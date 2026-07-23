# Code Review: PR 97 (Issue #37)

## 1. Decision
**Blocked.** The PR introduces a fundamental logic flaw where `dry_run` and CLI reporting always assume an empty database, leading to dangerously inaccurate migration reports. Additionally, it explicitly skips importing events, claims, and assessments, failing to meet the core PR objective.

## 2. Blocking Findings
1. **Broken Idempotency and Conflict Detection Reporting**
   * **Location:** `scripts/research_store/catalog_import.py:586-677` (`dry_run`) and `cli.py:261-269`.
   * **Details:** `CatalogImportService.dry_run()` requires `existing_run_ids` and `existing_invocation_ids` to be passed in to detect skipped records or conflicts. However, neither `cli.py` nor `CatalogImportService.apply()` fetches this state from PostgreSQL before calling `dry_run()`. 
   * **Impact:** The tool ALWAYS assumes the database is empty. Repeated imports will report `100% inserted` instead of `skipped`, and `catalog_import_record_map` will falsely record `mapping_status = 'inserted'`. While `ON CONFLICT DO NOTHING` prevents duplicate rows, the migration audit trail is fundamentally corrupted.
2. **Missing Implementation for Events, Claims, and Assessments**
   * **Location:** `scripts/research_store/catalog_import.py:722-764` and `1060-1063`.
   * **Details:** The PR objective explicitly states: *"Provide a dry-run-first migration utility for retained run, invocation, event, claim, and assessment files."* However, the code hardcodes these types to `status="pending"` (which gets mapped to `"omitted"`), stating they "require additional context". `_insert_record` explicitly raises `ImportApplyError` if it encounters them. 
   * **Impact:** The migration utility is incomplete.

## 3. Nonblocking Findings
* **Pending vs. Omitted:** In `catalog_import.py:814-824`, pending records are correctly warned about, but silently dumping them into the `omitted` bucket in `catalog_import_record_map` might make it hard to distinguish them from genuinely missing assets later. 

## 4. Acceptance-criteria matrix

| Criterion | Implementation Location | Test Evidence | Status |
| :--- | :--- | :--- | :--- |
| Dry run is default | `cli.py:223-228` | `test_catalog_import_cli_dry_run` | **Satisfied** |
| Repeated imports are idempotent | `catalog_import.py:1005-1044` | Unit tests only | **Partial** (DB inserts are idempotent, but audit tracking and CLI reports are completely wrong) |
| Conflicts require explicit resolution | `catalog_import.py:890-903` | Unit tests only | **Missing** (Because DB state is never fetched, conflicts are never detected in practice) |
| Source files are never deleted automatically | `catalog_import.py` (No file deletions) | Code inspection | **Satisfied** |
| Import events, claims, and assessments | `catalog_import.py:722-764` | None | **Missing** |

## 5. Authority and split-brain assessment
* **PostgreSQL Authority:** The PR correctly treats PostgreSQL as the sole authority, importing data without creating a secondary source of truth.
* **Split-brain risk:** None directly introduced. However, because events, claims, and assessments are reported as "omitted" rather than imported, users might incorrectly assume their migration is complete and delete old catalogs, leading to data loss.

## 6. Tests not demonstrated
* **Integration Idempotency:** There is no integration test calling `apply()` twice against the real database to verify that `records_skipped` correctly increments and `records_inserted` is `0` on the second run. 
* **Integration Conflict Detection:** No test simulates a scenario where PostgreSQL contains a newer run, proving `apply()` actually detects the conflict and refuses to overwrite.
* **Full Data Types:** No tests verify importing events, claims, or assessments.

## 7. Specific recommended changes
1. **Fix `dry_run` State Hydration:** `CatalogImportService` has a `uow_factory`. `dry_run()` should open a database connection and fetch `existing_run_ids` and `existing_invocation_ids` itself, rather than relying on the caller to pass them. This guarantees the CLI and `apply()` always have accurate reports.
2. **Implement Missing Types:** Implement the actual PostgreSQL mapping for events, claims, and assessments in `_dry_run_map_record` and `_insert_record`. If the needed "context" (e.g., `run_id` for events) can be inferred from the catalog directory structure or parent records within the same import, resolve it during the scan phase. 
3. **Add Integration Tests:** Add a test in `scripts/test_research_store_integration.py` that runs `apply()` on a catalog, and then runs it again, asserting that the second `ImportReport` has `records_skipped > 0` and `records_inserted == 0`.

## 8. Whether `Closes #37` is justified
**No.** The PR is incomplete (missing events, claims, assessments) and its core safety features (dry-run reporting and conflict detection) are non-functional in practice due to the state hydration bug.
