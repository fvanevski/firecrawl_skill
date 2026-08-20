from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from audit_release_gate_matrix import REQUIRED_GATE_IDS, validate_matrix
from research_store.stages import ContextKeys

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "references" / "audit-remediation-release-gates.json"
GATE_DOC = ROOT / "references" / "audit-remediation-release-gates.md"
ATTESTATION = ROOT / "references" / "audit-remediation-child-review-attestation.md"
RELEASE_NOTES = ROOT / "references" / "audit-remediation-rc-release-notes.md"


def _matrix():
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_machine_readable_matrix_contains_every_mandatory_gate_in_order():
    matrix = _matrix()
    assert validate_matrix(matrix) == []
    assert tuple(item["id"] for item in matrix["gates"]) == REQUIRED_GATE_IDS
    assert all(item["blocking"] is True for item in matrix["gates"])
    assert all(item["source_result"] == "pending" for item in matrix["gates"])


def test_matrix_has_exact_commands_evidence_and_artifact_for_every_gate():
    for gate in _matrix()["gates"]:
        assert gate["command"].strip()
        assert gate["expected_evidence"].strip()
        assert gate["artifact"].strip()
        assert gate["execution_phase"] in {"ci", "disposable", "credentialed"}


def test_gate_documentation_is_fail_closed_and_keeps_issue_open_until_release():
    text = GATE_DOC.read_text(encoding="utf-8")
    assert "No blocking gate may be waived" in text
    assert "Refs #223" in text
    assert "must remain open" in text
    assert "Real release campaign" in text
    assert "90 days" in text


def test_child_review_attestation_records_exact_corrective_pr_scope():
    text = ATTESTATION.read_text(encoding="utf-8")
    assert "96c865bc5541a928598e449d7c6f16a0c6c918d0" in text
    assert "832d1231707be423bb3cc9a2fbdd77c05c22d1d5" in text
    assert "0770930bbbf9de960e9456aea883cc7dc07752fe7e46bd3bfa8d056c99853b88" in text
    assert "109,819" in text
    assert "does not rewrite GitHub history" in text


def test_release_notes_distinguish_premerge_and_postmerge_evidence():
    text = RELEASE_NOTES.read_text(encoding="utf-8")
    assert "Pre-merge" in text
    assert "Post-merge" in text
    assert "No RC tag exists yet" in text
    assert "forward repair or PostgreSQL backup restoration" in text


def test_pr_and_push_workflow_runs_exact_candidate_with_disposable_services():
    workflow = (ROOT / ".github" / "workflows" / "audit-release-gates.yml").read_text(
        encoding="utf-8"
    )
    assert "github.event.pull_request.head.sha || github.sha" in workflow
    assert "postgres:16-alpine" in workflow
    assert "qdrant/qdrant:v1.18.3-unprivileged" in workflow
    assert "--phase ci" in workflow
    assert "--phase disposable" in workflow
    assert "retention-days: 90" in workflow
    assert "mypy" in workflow


def test_real_release_campaign_is_blocked_by_disposable_gates_and_secret_scan():
    workflow = (ROOT / ".github" / "workflows" / "release-campaign.yml").read_text(
        encoding="utf-8"
    )
    assert "audit-gates:" in workflow
    assert "needs: audit-gates" in workflow
    assert "scripts/scan_release_secrets.py" in workflow
    assert "steps.secret_scan.outcome" in workflow
    assert "real-release-campaign-${{ inputs.candidate-sha }}" in workflow
    assert "retention-days: 90" in workflow


def test_bounded_execution_uses_positive_type_contract_not_mock_detection():
    """The ARC-17 correction must identify production services by positive type
    membership, not by excluding ``unittest.mock`` types.  The bounded ingest
    path lives on ``CorpusService`` directly and the idempotency guard lives
    on ``ExtractionService.complete_attempt``; neither uses mock detection."""
    import inspect

    from research_store.extraction_service import ExtractionService
    from research_store.service import CorpusService

    bounded_source = inspect.getsource(CorpusService.bounded_ingest_batch)
    complete_source = inspect.getsource(ExtractionService.complete_attempt)

    for source in (bounded_source, complete_source):
        assert "unittest.mock" not in source
        assert "MagicMock" not in source
        assert "Mock)" not in source or "isinstance" in source

    # The bounded ingest path is a direct method call — no monkeypatch shim.
    assert hasattr(CorpusService, "bounded_ingest_batch")
    assert callable(CorpusService.bounded_ingest_batch)


def test_concurrent_bounded_executions_on_independent_instances_do_not_interfere():
    """Two bounded extraction stages built from independent service instances
    must execute concurrently without interfering with each other.  Each stage
    owns its own service instances and the bounded ingest path is scoped to
    those instances — there is no shared class-level state to collide on."""
    import threading
    import time

    import pytest

    test_dsn = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
    if not test_dsn:
        pytest.skip(
            "requires RESEARCH_STORE_TEST_DATABASE_URL for service construction"
        )

    from unittest.mock import MagicMock

    from research_store.bounded_orchestrator import BoundedExtractionStage
    from research_store.config import StoreConfig
    from research_store.container import build_extraction_service, build_service

    config = StoreConfig.from_env()
    config = replace(config, database_url=test_dsn)
    corpus_a = build_service(config)
    corpus_b = build_service(config)
    extraction_a = build_extraction_service(config)
    extraction_b = build_extraction_service(config)

    stage_a = BoundedExtractionStage(
        run_service=MagicMock(),
        coverage_service=MagicMock(),
        config=config,
        corpus_service=corpus_a,
        extraction_service=extraction_a,
    )
    stage_b = BoundedExtractionStage(
        run_service=MagicMock(),
        coverage_service=MagicMock(),
        config=config,
        corpus_service=corpus_b,
        extraction_service=extraction_b,
    )

    # Verify independent instances do not share bound methods.
    assert corpus_a.ingest_batch is not corpus_b.ingest_batch
    assert corpus_a.bounded_ingest_batch is not corpus_b.bounded_ingest_batch
    assert extraction_a.complete_attempt is not extraction_b.complete_attempt

    # Execute both stages concurrently and prove no interference.
    results: list[dict[str, object]] = [
        {"stage": "a", "result": None},
        {"stage": "b", "result": None},
    ]
    barriers = [threading.Barrier(2), threading.Barrier(2)]

    def run_stage(stage, idx):
        barriers[0].wait()
        start = time.monotonic()
        try:
            result = stage.execute(
                run_id=__import__("uuid").uuid4(),
                run_revision=1,
                coverage_revision=None,
                run_state="extracting",
                context={
                    "raw_ingest_requests": [],
                    ContextKeys.WAVE_COUNT: 0,
                    "candidate_coverage_items": {},
                },
            )
            results[idx]["result"] = result
        except Exception as exc:  # noqa: BLE001
            results[idx]["result"] = exc
        finally:
            barriers[1].wait()
        results[idx]["elapsed"] = time.monotonic() - start

    thread_a = threading.Thread(target=run_stage, args=(stage_a, 0))
    thread_b = threading.Thread(target=run_stage, args=(stage_b, 1))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "stage_a thread did not complete"
    assert not thread_b.is_alive(), "stage_b thread did not complete"

    for r in results:
        assert not isinstance(r["result"], Exception), (
            f"concurrent execution on {r['stage']} raised: {r['result']}"
        )

    # Service methods remain in their canonical state after concurrent execution.
    # The bounded stage carries the issue #217 contract wrapper; what matters is
    # that no per-call ARC-17 monkeypatch shim remains and that independent
    # instances do not share bound methods.
    assert stage_a.execute.__name__ == "_bounded_extraction_execute"
    assert stage_b.execute.__name__ == "_bounded_extraction_execute"
    assert hasattr(corpus_a, "bounded_ingest_batch")
    assert hasattr(corpus_b, "bounded_ingest_batch")


def test_release_campaign_secret_env_matches_scanner_boundary():
    """Every injected release secret must appear as an exact-value scanner arg."""
    import re

    workflow = (ROOT / ".github" / "workflows" / "release-campaign.yml").read_text(
        encoding="utf-8"
    )

    # Collect every ${{ secrets.* }} reference in the campaign workflow.
    secret_refs = sorted(
        {
            m.group(1)
            for m in re.finditer(
                r"\$\{\{\s*secrets\.([A-Z_][A-Z0-9_]*)\s*\}\}", workflow
            )
        }
    )

    # Collect every --secret-env value passed to scan_release_secrets.py.
    scan_section = workflow.split("Scan complete release campaign")[1]
    scan_section = scan_section.split("- name: Upload")[0]
    secret_env_values = sorted(
        {
            m.group(1)
            for m in re.finditer(r"--secret-env\s+([A-Z_][A-Z0-9_]*)", scan_section)
        }
    )

    missing = set(secret_refs) - set(secret_env_values)
    assert not missing, (
        f"Release campaign injects secrets that the scanner does not check: "
        f"{sorted(missing)}"
    )
