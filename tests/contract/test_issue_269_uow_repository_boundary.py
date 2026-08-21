"""Final UoW/repository ownership regressions for issue #269 gate closure."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
PORTS = STORE / "ports.py"
POSTGRES = STORE / "postgres.py"
UOW_CORE = STORE / "postgres_uow_core.py"

TEST_AUTHORITY_PATHS = (
    ROOT / "tests/integration/test_research_store_integration.py",
    ROOT / "tests/integration/test_arc17_corrective_defects.py",
    ROOT / "tests/unit/test_handoff.py",
    ROOT / "tests/unit/test_report_service.py",
)

# These are behavioral API exceptions, not generic persistence routing. The
# transaction methods are UoW infrastructure; persist_ingest and the six #217
# methods are documented compatibility contracts installed on the UoW class.
ALLOWED_DIRECT_UOW_CALLS = frozenset(
    {
        "commit",
        "rollback",
        "savepoint",
        "execute",
        "fetchone",
        "persist_ingest",
        "start_ingestion_batch",
        "record_batch_asset",
        "finish_ingestion_batch",
        "export_invocation",
        "export_invocation_by_batch",
    }
)

DIRECT_UOW_COMPATIBILITY_ANNOTATIONS = frozenset(
    {
        "persist_ingest",
        "start_ingestion_batch",
        "record_batch_asset",
        "finish_ingestion_batch",
        "export_invocation",
        "export_invocation_by_batch",
    }
)

# ResearchRunRepository owns lifecycle/invocation/event/spec/budget state only.
# These operations belong to other named repositories and must never be routed
# through uow.runs again.
FORBIDDEN_RUN_REPOSITORY_CALLS = frozenset(
    {
        "record_search_plan",
        "get_search_plan",
        "list_search_plans",
        "get_plan_query",
        "list_plan_queries",
        "record_search_response",
        "get_search_response",
        "list_search_responses",
        "open_raw_search_response_blob",
        "record_response_candidates",
        "get_candidate",
        "list_candidates",
        "list_candidates_paginated",
        "list_candidate_occurrences",
        "assign_duplicate_group",
        "persist_duplicate_group",
        "update_candidate_independence",
        "record_rankings",
        "create_attempt",
        "complete_attempt",
        "update_disposition",
        "record_quality_metrics",
        "select_final_attempt",
        "get_selected_attempt",
        "list_attempts_for_candidate",
        "list_attempts_for_run",
        "get_attempt",
        "record_semantic_call",
        "finalize_semantic_call",
        "annotate_semantic_call",
        "get_semantic_call",
        "record_semantic_artifact",
    }
)


def _source_files() -> list[Path]:
    return sorted(STORE.rglob("*.py"))


def _call_chain(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def test_production_uses_named_repositories_not_direct_uow_domain_methods() -> None:
    violations: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _call_chain(node.func)
            if chain is None or len(chain) != 2 or chain[0] != "uow":
                continue
            operation = chain[1]
            if operation not in ALLOWED_DIRECT_UOW_CALLS:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} calls uow.{operation}()"
                )
    assert violations == [], "direct UoW domain routing remains:\n" + "\n".join(
        violations
    )


def test_critical_test_authorities_use_named_repositories() -> None:
    """Keep broad integration/report fixtures aligned with the production UoW boundary."""
    violations: list[str] = []
    for path in TEST_AUTHORITY_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _call_chain(node.func)
            if chain is None or len(chain) != 2 or chain[0] != "uow":
                continue
            operation = chain[1]
            if operation not in ALLOWED_DIRECT_UOW_CALLS:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} calls uow.{operation}()"
                )
    assert violations == [], (
        "stale direct UoW calls remain in test authority:\n" + "\n".join(violations)
    )


def test_run_repository_is_not_a_cross_domain_router() -> None:
    violations: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _call_chain(node.func)
            if (
                chain is not None
                and len(chain) == 3
                and chain[:2] == ("uow", "runs")
                and chain[2] in FORBIDDEN_RUN_REPOSITORY_CALLS
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} calls "
                    f"uow.runs.{chain[2]}()"
                )
    assert violations == [], "cross-domain uow.runs routing remains:\n" + "\n".join(
        violations
    )


def test_generic_compatibility_router_is_absent_from_uow_core() -> None:
    source = UOW_CORE.read_text(encoding="utf-8")
    forbidden_markers = (
        "class _RunsRepository",
        "_make_uow_compatibility_delegate",
        "_bind_uow_compatibility_delegate",
        "_COMPATIBILITY_OPERATIONS",
        "direct_compatibility_sets",
        "runs_legacy_repositories",
    )
    remaining = [marker for marker in forbidden_markers if marker in source]
    assert remaining == [], f"generic compatibility router markers remain: {remaining}"
    assert '"runs": self.__research_repository' in source


def test_postgres_uow_static_callable_surface_matches_published_exceptions() -> None:
    """Do not advertise direct domain APIs that runtime composition does not install."""
    tree = ast.parse(POSTGRES.read_text(encoding="utf-8"), filename=str(POSTGRES))
    uow_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PostgresUnitOfWork"
    )
    callable_annotations = {
        node.target.id
        for node in uow_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and ast.unparse(node.annotation).startswith("Callable[")
    }
    assert callable_annotations == DIRECT_UOW_COMPATIBILITY_ANNOTATIONS


def test_ports_encode_separate_repository_roles() -> None:
    tree = ast.parse(PORTS.read_text(encoding="utf-8"), filename=str(PORTS))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    run_repository = classes["ResearchRunRepository"]
    run_bases = {base.id for base in run_repository.bases if isinstance(base, ast.Name)}
    assert run_bases == {"Protocol"}

    search_repository = classes["SearchAcquisitionRepository"]
    search_bases = {
        base.id for base in search_repository.bases if isinstance(base, ast.Name)
    }
    assert search_bases == {"SearchResponseRepository", "Protocol"}

    unit_of_work = classes["UnitOfWork"]
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for node in unit_of_work.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert annotations["runs"] == "ResearchRunRepository"
    assert annotations["search_responses"] == "SearchAcquisitionRepository"
    assert annotations["candidates"] == "CandidateRepository"
    assert annotations["semantic_calls"] == "SemanticCallRepository"
    assert annotations["retrieval_events"] == "RetrievalEventRepository"


def test_issue_217_internal_asset_link_uses_snapshot_repository() -> None:
    source = (STORE / "ingestion_batch_semantics.py").read_text(encoding="utf-8")
    assert "uow.snapshots.link_run_asset(" in source
    assert "uow.link_run_asset(" not in source
