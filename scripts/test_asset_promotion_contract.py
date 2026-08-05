"""Static contracts for issue #211 staged promotion and sealed membership."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
STORE = SCRIPTS / "research_store"
MIGRATION = STORE / "alembic" / "versions" / "0040_asset_promotion_membership.py"
MIGRATION_SQL = tuple(
    sorted((MIGRATION.parent).glob("0040_asset_promotion_membership_*.sql"))
)
REFERENCE = SCRIPTS.parent / "references" / "asset-promotion-membership.md"
STAGES = (
    "discovered",
    "selected_for_extraction",
    "extracted",
    "retained",
    "evidence_eligible",
    "completion_critical",
    "rejected",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _migration_source() -> str:
    return _source(MIGRATION) + "\n" + "\n".join(map(_source, MIGRATION_SQL))


def _service_source() -> str:
    paths = (
        STORE / "asset_promotion_service.py",
        STORE / "asset_promotion_models.py",
        STORE / "asset_promotion_core.py",
        STORE / "asset_promotion_seal.py",
        STORE / "asset_promotion_store.py",
    )
    return "\n".join(map(_source, paths))


def _checkpoint_source() -> str:
    paths = (
        STORE / "index_checkpoint_service.py",
        STORE / "index_checkpoint_core.py",
        STORE / "index_checkpoint_finalize.py",
        STORE / "index_checkpoint_asset_membership.py",
    )
    return "\n".join(map(_source, paths))


def test_migration_is_linear_additive_and_forward_only():
    source = _migration_source()
    assert 'revision = "0040_asset_promotion_membership"' in source
    assert 'down_revision = "0039_index_checkpoint_guard"' in source
    assert "CREATE TABLE run_asset_promotion_subjects" in source
    assert "CREATE TABLE run_asset_promotion_events" in source
    assert "CREATE TABLE run_asset_membership_seals" in source
    assert "CREATE TABLE run_asset_membership_members" in source
    assert "ALTER TABLE indexing_checkpoints" in source
    assert source.count("END;\n        $function$;") >= 13
    assert "%%ROWTYPE" not in source
    assert "forward repair" in source


def test_all_required_stages_and_ordered_transitions_are_database_enforced():
    source = _migration_source()
    for stage in STAGES:
        assert stage in source
    for transition in (
        "OLD.current_stage='discovered'",
        "NEW.current_stage IN ('selected_for_extraction','rejected')",
        "OLD.current_stage='selected_for_extraction'",
        "NEW.current_stage IN ('extracted','rejected')",
        "OLD.current_stage='extracted'",
        "NEW.current_stage IN ('retained','rejected')",
        "OLD.current_stage='retained'",
        "NEW.current_stage IN ('evidence_eligible','rejected')",
        "OLD.current_stage='evidence_eligible'",
        "NEW.current_stage IN ('completion_critical','rejected')",
    ):
        assert transition in source
    assert "invalid asset promotion transition" in source


def test_promotion_provenance_records_actor_policy_revision_time_and_reason():
    source = _migration_source()
    for field in (
        "actor_type",
        "actor_identifier",
        "policy_version",
        "lifecycle_revision",
        "reason_code",
        "reason",
        "occurred_at",
        "transaction_id",
    ):
        assert field in source
    assert "run_asset_promotion_events_append_only_trigger" in source


def test_extraction_stops_before_evidence_and_completion_membership():
    source = _migration_source()
    function = source.split(
        "CREATE FUNCTION record_extraction_promotion_stages()", 1
    )[1]
    function = function.split(
        "CREATE TRIGGER extraction_attempt_initializes", 1
    )[0]
    assert "selected_for_extraction" in function
    assert "extracted" in function
    assert "NEW.end_time IS NOT NULL" in function
    assert "NEW.raw_blob_sha256 IS NOT NULL" in function
    assert "NEW.normalized_blob_sha256 IS NOT NULL" in function
    assert "evidence_eligible" not in function
    assert "completion_critical" not in function
    assert "AFTER UPDATE OF exit_status,end_time" in source
    assert "guard_extraction_attempt_after_promotion" in source


def test_seal_binds_only_completion_critical_assets_and_exact_chunks():
    service = _service_source()
    checkpoint = _checkpoint_source()
    migration = _migration_source()
    assert "subject.current_stage='completion_critical'" in service
    assert "chunk_ids" in service
    assert "membership_sha256" in service
    assert "expected_chunk_count" in service
    assert "prepared_asset_seal.chunk_ids" in checkpoint
    assert "indexing_checkpoint_binds_asset_membership_trigger" in migration


def test_postgresql_recomputes_member_hash_seal_hash_and_counts():
    migration = _migration_source()
    assert "canonical_asset_membership_member_payload" in migration
    assert "validate_run_asset_membership_seal" in migration
    assert "member SHA-256 does not address" in migration
    assert "seal SHA-256 does not address" in migration
    assert "expected asset count" in migration
    assert "expected chunk count" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration


def test_completed_checkpoint_replay_uses_the_bound_asset_seal():
    replay = _source(STORE / "index_checkpoint_replay.py")
    assert "load_active_seal_in_transaction" in replay
    assert "asset_seal.chunk_ids" in replay
    assert "_validate_asset_binding" in replay
    assert "_completion_payload" in replay
    assert "_current_membership" in replay


def test_post_seal_membership_change_requires_explicit_reopen_and_revision_cas():
    migration = _migration_source()
    service = _service_source()
    assert "reopen it before changing membership" in migration
    assert "reopen_completion_membership" in service
    assert "expected_lifecycle_revision" in service
    assert "asset_membership_reopened" in migration


def test_historical_compatibility_does_not_infer_stage_history():
    service = _service_source()
    migration = _migration_source()
    assert "legacy_unstructured" in service
    assert "historical_stage_unknown" in service
    assert "no history was inferred" in migration
    assert "apply an evidence-bearing forward repair" in service


def test_promotion_and_concurrency_code_has_no_arbitrary_sleep():
    source = _service_source()
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "sleep" not in calls
    assert "_after_membership_lock" in source
    assert "_after_promotion_step" in source


def test_asset_promotion_modules_import_without_default_argument_name_errors():
    importlib.import_module("research_store.asset_promotion_service")
    importlib.import_module("research_store.index_checkpoint_service")


def test_dedicated_workflow_runs_contract_and_postgres_integration_tests():
    workflow = _source(
        SCRIPTS.parent / ".github" / "workflows" / "index-checkpoint.yml"
    )
    assert "scripts/test_asset_promotion_contract.py" in workflow
    assert "scripts/test_asset_promotion_integration.py" in workflow


def test_reference_documents_authority_stages_reopen_and_compatibility():
    reference = _source(REFERENCE)
    for stage in STAGES:
        assert stage in reference
    assert "Qdrant remains a rebuildable vector" in reference
    assert "explicitly reopen" in reference
    assert "legacy_unstructured" in reference
    assert "No earlier stage" in reference
    assert "finalized successful extraction" in reference
    assert "recomputes and verifies" in reference
