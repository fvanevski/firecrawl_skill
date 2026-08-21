#!/usr/bin/env python3
"""One-shot mechanical migration for PR #296 test authority.

This script is committed only on the bootstrap revision used by the branch-scoped
workflow-dispatch job.  The job removes this script and restores ``ci.yml`` from
the bootstrap parent before committing the actual test migration.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "tests" / "integration" / "test_research_store_integration.py"
CONTRACT = ROOT / "tests" / "contract" / "test_issue_269_uow_repository_boundary.py"

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
        "get_trace",
    }
)

ROLE_BY_METHOD = {
    # Research workflow lifecycle, invocation/event journal, specs, and budgets.
    "start_run": "runs",
    "get_run_status": "runs",
    "record_invocation": "runs",
    "get_invocation_status": "runs",
    "list_invocations": "runs",
    "append_event": "runs",
    "get_event_by_id": "runs",
    "list_events": "runs",
    "next_event_sequence": "runs",
    "record_research_spec": "runs",
    "record_budget_snapshot": "runs",
    "get_research_spec": "runs",
    "get_latest_budget_snapshot": "runs",
    "append_run_transition": "runs",
    "apply_run_transition": "runs",
    "revise_execution_mode": "runs",
    "count_acquisition_waves": "runs",
    # Corpus/query roles.
    "link_run_asset": "snapshots",
    "search_lexical": "documents",
    "fetch_passages": "documents",
    "fetch_run_passages": "documents",
    "inspect_asset": "documents",
    "expand_relationships": "documents",
    # Retrieval and indexing roles.
    "record_retrieval_execution": "retrieval_events",
    "log_retrieval_batch": "retrieval_events",
    "claim_jobs": "index_jobs",
    "finish_job": "index_jobs",
    "fail_job": "index_jobs",
    "renew_job_lease": "index_jobs",
    # Search-acquisition roles.
    "record_search_plan": "search_responses",
    "get_search_plan": "search_responses",
    "list_search_plans": "search_responses",
    "get_plan_query": "search_responses",
    "list_plan_queries": "search_responses",
    "record_search_response": "search_responses",
    "get_search_response": "search_responses",
    "list_search_responses": "search_responses",
    "open_raw_search_response_blob": "search_responses",
    "record_response_candidates": "candidates",
    "get_candidate": "candidates",
    "list_candidates": "candidates",
    "list_candidates_paginated": "candidates",
    "list_candidate_occurrences": "candidates",
    "assign_duplicate_group": "candidates",
    "persist_duplicate_group": "candidates",
    "update_candidate_independence": "candidates",
    "record_rankings": "candidates",
    "create_attempt": "extraction_attempts",
    "complete_attempt": "extraction_attempts",
    "update_disposition": "extraction_attempts",
    "record_quality_metrics": "extraction_attempts",
    "select_final_attempt": "extraction_attempts",
    "get_selected_attempt": "extraction_attempts",
    "list_attempts_for_candidate": "extraction_attempts",
    "list_attempts_for_run": "extraction_attempts",
    "get_attempt": "extraction_attempts",
    # Semantic/evidence/reporting roles.
    "record_semantic_call": "semantic_calls",
    "finalize_semantic_call": "semantic_calls",
    "annotate_semantic_call": "semantic_calls",
    "get_semantic_call": "semantic_calls",
    "record_semantic_artifact": "semantic_calls",
    "persist_evidence_packet": "evidence_packets",
    "get_evidence_packet": "evidence_packets",
    "insert_synthesis_stage": "synthesis_stages",
    "update_synthesis_stage": "synthesis_stages",
    "get_synthesis_stage": "synthesis_stages",
    "get_synthesis_stages": "synthesis_stages",
}

TEST_AUTHORITY_PATHS = (
    "tests/integration/test_research_store_integration.py",
    "tests/integration/test_arc17_corrective_defects.py",
    "tests/unit/test_handoff.py",
    "tests/unit/test_report_service.py",
)


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


def _direct_uow_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _call_chain(node.func)
        if chain is not None and len(chain) == 2 and chain[0] == "uow":
            calls.append((node.lineno, chain[1]))
    return sorted(calls)


def _migrate_integration() -> None:
    source = INTEGRATION.read_text(encoding="utf-8")
    before = _direct_uow_calls(INTEGRATION)
    unknown = sorted(
        {
            operation
            for _line, operation in before
            if operation not in ALLOWED_DIRECT_UOW_CALLS
            and operation not in ROLE_BY_METHOD
        }
    )
    if unknown:
        inventory = ", ".join(f"{line}:uow.{op}" for line, op in before)
        raise SystemExit(
            "unmapped direct UoW calls in integration authority: "
            f"{unknown}; inventory={inventory}"
        )

    replacements: dict[str, int] = {}
    for operation, role in ROLE_BY_METHOD.items():
        pattern = rf"\buow\.{re.escape(operation)}(?=\s*\()"
        source, count = re.subn(pattern, f"uow.{role}.{operation}", source)
        if count:
            replacements[f"{role}.{operation}"] = count

    # Correct the one explanatory comment whose old wording would otherwise
    # continue to describe a direct domain method on the UoW.
    source = source.replace(
        "directly through the\n    unit-of-work to confirm the database constraint",
        "directly through the\n    synthesis-stages repository to confirm the database constraint",
    )
    INTEGRATION.write_text(source, encoding="utf-8")

    after = _direct_uow_calls(INTEGRATION)
    violations = [
        (line, operation)
        for line, operation in after
        if operation not in ALLOWED_DIRECT_UOW_CALLS
    ]
    if violations:
        raise SystemExit(f"direct UoW domain calls remain after migration: {violations}")
    if not replacements:
        raise SystemExit("migration made no repository-role replacements")
    print("repository-role replacements:")
    for key, count in sorted(replacements.items()):
        print(f"  {key}: {count}")


def _patch_contract() -> None:
    source = CONTRACT.read_text(encoding="utf-8")
    if "TEST_AUTHORITY_PATHS = (" not in source:
        anchor = 'UOW_CORE = STORE / "postgres_uow_core.py"\n'
        insertion = anchor + "\nTEST_AUTHORITY_PATHS = (\n" + "".join(
            f'    ROOT / "{path}",\n' for path in TEST_AUTHORITY_PATHS
        ) + ")\n"
        if anchor not in source:
            raise SystemExit("contract insertion anchor for TEST_AUTHORITY_PATHS not found")
        source = source.replace(anchor, insertion, 1)

    test_name = "test_critical_test_authorities_use_named_repositories"
    if f"def {test_name}" not in source:
        anchor = "\ndef test_run_repository_is_not_a_cross_domain_router() -> None:\n"
        new_test = '''\n\ndef test_critical_test_authorities_use_named_repositories() -> None:\n    """Keep broad integration/report fixtures aligned with the production UoW boundary."""\n    violations: list[str] = []\n    for path in TEST_AUTHORITY_PATHS:\n        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))\n        for node in ast.walk(tree):\n            if not isinstance(node, ast.Call):\n                continue\n            chain = _call_chain(node.func)\n            if chain is None or len(chain) != 2 or chain[0] != "uow":\n                continue\n            operation = chain[1]\n            if operation not in ALLOWED_DIRECT_UOW_CALLS:\n                violations.append(\n                    f"{path.relative_to(ROOT)}:{node.lineno} calls uow.{operation}()"\n                )\n    assert violations == [], "stale direct UoW calls remain in test authority:\\n" + "\\n".join(\n        violations\n    )\n'''
        if anchor not in source:
            raise SystemExit("contract insertion anchor for test-authority regression not found")
        source = source.replace(anchor, new_test + anchor, 1)

    CONTRACT.write_text(source, encoding="utf-8")


def _verify() -> None:
    violations: list[str] = []
    for relative in TEST_AUTHORITY_PATHS:
        path = ROOT / relative
        for line, operation in _direct_uow_calls(path):
            if operation not in ALLOWED_DIRECT_UOW_CALLS:
                violations.append(f"{relative}:{line}: uow.{operation}()")
    if violations:
        raise SystemExit("stale direct UoW test authority:\n" + "\n".join(violations))

    contract_source = CONTRACT.read_text(encoding="utf-8")
    if "test_critical_test_authorities_use_named_repositories" not in contract_source:
        raise SystemExit("test-authority AST regression was not installed")
    integration_source = INTEGRATION.read_text(encoding="utf-8")
    required_examples = (
        "uow.runs.start_run(",
        "uow.runs.record_research_spec(",
        "uow.semantic_calls.record_semantic_call(",
        "uow.documents.search_lexical(",
        "uow.index_jobs.claim_jobs(",
        "uow.search_responses.list_plan_queries(",
        "uow.synthesis_stages.insert_synthesis_stage(",
    )
    missing = [item for item in required_examples if item not in integration_source]
    if missing:
        raise SystemExit(f"expected named repository examples are missing: {missing}")
    print("test-authority repository boundary verified")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.apply == args.verify:
        parser.error("choose exactly one of --apply or --verify")
    if args.apply:
        _migrate_integration()
        _patch_contract()
    _verify()


if __name__ == "__main__":
    main()
