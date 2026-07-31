"""Contract tests for authoritative full-campaign dispatch."""

from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "release-campaign.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _inline_verifier(text: str) -> str:
    opening = "          python - <<'PY'\n"
    closing = "          PY\n"
    start = text.index(opening) + len(opening)
    end = text.index(closing, start)
    return "\n".join(
        line.removeprefix("          ") for line in text[start:end].splitlines()
    )


def test_release_workflow_binds_exact_candidate():
    workflow = _workflow_text()
    assert '--candidate-sha "$CANDIDATE_SHA"' in workflow
    assert 'ref: ${{ inputs.candidate-sha }}' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in workflow


def test_release_workflow_enforces_two_mode_campaign_shape():
    workflow = _workflow_text()
    assert 'SMOKE_DISABLE_AGENT_LED: "1"' in workflow
    assert 'expected_modes = ("autonomous_local", "deterministic_debug")' in workflow
    assert 'campaign {label} expected 10 runs' in workflow
    assert '20 globally unique run UUIDs' in workflow


def test_release_workflow_verifier_is_valid_python():
    compile(_inline_verifier(_workflow_text()), str(WORKFLOW), "exec")


def test_release_workflow_retains_hash_bound_artifacts_for_90_days():
    workflow = _workflow_text()
    assert '"schema_version": "authoritative-release-evidence-v1"' in workflow
    assert '"manifest_sha256": sha256(evidence_path)' in workflow
    assert "retention-days: 90" in workflow
