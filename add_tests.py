import re

def append_tests():
    with open("scripts/test_research_store_integration.py", "r") as f:
        content = f.read()

    new_tests = """
def test_catalog_import_apply_idempotency_integration(tmp_path, monkeypatch):
    \"\"\"Verify apply() idempotency on a second run correctly skips records.\"\"\"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)

    from research_store.catalog_import import CatalogImportService
    from research_store.container import build_catalog_import_service
    import json

    service = build_catalog_import_service()
    import_svc = CatalogImportService(
        service.uow_factory if hasattr(service, "uow_factory") else service._uow_factory
    )

    root = tmp_path / "catalog"
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True)
    run_id = "fr_" + "i" * 32
    run_data = {
        "schema_version": 5,
        "research_run_id": run_id,
        "objective": "Test idempotency integration",
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

    # First run
    report1 = import_svc.apply(root)
    assert report1.records_inserted == 1
    assert report1.records_skipped == 0

    # Second run
    report2 = import_svc.apply(root)
    assert report2.records_inserted == 0
    assert report2.records_skipped == 1

def test_catalog_import_apply_conflict_detection_integration(tmp_path, monkeypatch):
    \"\"\"Verify apply() correctly detects and reports conflicts.\"\"\"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)

    from research_store.catalog_import import CatalogImportService
    from research_store.container import build_catalog_import_service
    from research_store.run_service import ResearchStoreService
    import json

    service = build_catalog_import_service()
    import_svc = CatalogImportService(
        service.uow_factory if hasattr(service, "uow_factory") else service._uow_factory
    )

    # First we need to populate a run in DB
    run_id = "fr_" + "c" * 32
    uow_f = service.uow_factory if hasattr(service, "uow_factory") else service._uow_factory
    with uow_f() as uow:
        cur = uow.connection.cursor()
        cur.execute(
            \"\"\"INSERT INTO research_runs (external_run_id, status, lifecycle_revision)
            VALUES (%s, %s, %s)\"\"\",
            (run_id, "completed", 5)
        )
        uow.connection.commit()

    root = tmp_path / "catalog"
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True)
    
    # Run in catalog has older revision/different data, representing a conflict (or just existing)
    # Actually, the logic in _dry_run_map_record says "skipped" if it exists. 
    # But wait, earlier we said if it exists it returns skipped with conflict_detail="PostgreSQL run already exists; Catalog is older".
    run_data = {
        "schema_version": 5,
        "research_run_id": run_id,
        "objective": "Test conflict integration",
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

    report = import_svc.apply(root)
    assert report.records_inserted == 0
    assert report.records_skipped == 1
    assert report.mappings[0].conflict_detail is not None
"""
    if "test_catalog_import_apply_idempotency_integration" not in content:
        with open("scripts/test_research_store_integration.py", "a") as f:
            f.write(new_tests)

append_tests()
