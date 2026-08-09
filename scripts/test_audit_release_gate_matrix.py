from __future__ import annotations

import json
from pathlib import Path

from audit_release_gate_matrix import REQUIRED_GATE_IDS, validate_matrix

ROOT = Path(__file__).parents[1]
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
