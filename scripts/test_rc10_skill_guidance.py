"""Validate RC-10 agent guidance against executable and parser contracts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
SKILL_PATH = SKILL_ROOT / "SKILL.md"
RUN_ID = "fr_" + "a" * 32
OTHER_RUN_ID = "fr_" + "b" * 32

RC10_REFERENCES = {
    "references/release-candidate-gate-rc10.md": (
        "RC-10 Aggregate Release-Candidate Gate",
        "exact resulting `main` SHA",
        "Real release campaign",
        "candidate SHA, dispatch SHA, workflow SHA",
        "artifact ID",
        "artifact digest",
    ),
    "references/release-campaign-timing-diagnostics.md": (
        "release-campaign-timing-v2",
        "PostgreSQL remains authoritative",
        "telemetry_complete",
        "reproducibility_failures",
        "verify_release_campaign_strict.py",
    ),
}


def _skill_content() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _marked_shell(name: str) -> str:
    content = _skill_content()
    start = f"# {name}:start"
    end = f"# {name}:end"
    assert content.count(start) == 1
    assert content.count(end) == 1
    return content.split(start, 1)[1].split(end, 1)[0].strip()


def _write_fake_rtk(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    call_log = tmp_path / "rtk-calls.log"
    executable = binary_dir / "rtk"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$RTK_CALL_LOG"
[[ "${1:-}" == "proxy" ]] || exit 99
target="${2:-}"
if [[ "$target" == */fsearch_smart ]]; then
  case "${RTK_MODE:-checkpoint}" in
    checkpoint)
      printf '%s\n' \
        'Topic: documented topic' \
        'Run ID: fr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
        'Planning source: persisted' \
        'Orchestrator outcome: checkpoint' \
        'Final state: coverage_review'
      exit 75
      ;;
    resume)
      [[ -z "${FIRECRAWL_SMART_STOP_AFTER_STATE:-}" ]] || exit 98
      [[ "$*" == *"--research-run-id ${EXPECTED_RUN_ID}"* ]] || exit 97
      printf 'Run ID: %s\n' "$EXPECTED_RUN_ID"
      printf '%s\n' 'Orchestrator outcome: resumed'
      exit 0
      ;;
    *)
      exit 96
      ;;
  esac
fi
if [[ "$target" == */research-db && "${3:-}" == "run-status" ]]; then
  printf '{"external_id":"%s","state":"coverage_review"}\n' "${4:-}"
  exit 0
fi
exit 95
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return binary_dir, call_log


def _run_documented_shell(
    block: str,
    *,
    binary_dir: Path,
    call_log: Path,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(extra_env)
    environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
    environment["RTK_CALL_LOG"] = str(call_log)
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


@pytest.mark.parametrize("rel_path,required", RC10_REFERENCES.items())
def test_rc10_reference_exists_and_retains_contract(
    rel_path: str,
    required: tuple[str, ...],
) -> None:
    path = SKILL_ROOT / rel_path
    assert path.is_file(), f"missing RC-10 reference: {rel_path}"
    content = path.read_text(encoding="utf-8")
    assert content.strip(), f"empty RC-10 reference: {rel_path}"
    for phrase in required:
        assert phrase in content


def test_skill_links_both_rc10_references() -> None:
    content = _skill_content()
    for rel_path in RC10_REFERENCES:
        assert f"`{rel_path}`" in content


def test_documented_checkpoint_handler_is_one_shot_and_returns_75(
    tmp_path: Path,
) -> None:
    block = _marked_shell("fsearch-smart-checkpoint-handler")
    assert "while true" not in block
    assert "exit 75" in block

    binary_dir, call_log = _write_fake_rtk(tmp_path)
    result = _run_documented_shell(
        block,
        binary_dir=binary_dir,
        call_log=call_log,
        extra_env={
            "RTK_MODE": "checkpoint",
            "FIRECRAWL_SMART_STOP_AFTER_STATE": "coverage_review",
        },
    )

    assert result.returncode == 75, result.stderr
    assert "Checkpoint reached" in result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert sum("/fsearch_smart" in call for call in calls) == 1
    assert sum("/research-db run-status" in call for call in calls) == 1
    assert RUN_ID in calls[-1]


def test_documented_checkpoint_resume_reuses_run_and_clears_stop_control(
    tmp_path: Path,
) -> None:
    block = _marked_shell("fsearch-smart-checkpoint-resume")
    assert "unset FIRECRAWL_SMART_STOP_AFTER_STATE" in block
    assert '--research-run-id "$RUN_ID"' in block

    binary_dir, call_log = _write_fake_rtk(tmp_path)
    result = _run_documented_shell(
        block,
        binary_dir=binary_dir,
        call_log=call_log,
        extra_env={
            "RUN_ID": RUN_ID,
            "EXPECTED_RUN_ID": RUN_ID,
            "RTK_MODE": "resume",
            "FIRECRAWL_SMART_STOP_AFTER_STATE": "coverage_review",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "/fsearch_smart" in calls[0]
    assert f"--research-run-id {RUN_ID}" in calls[0]


def test_skill_lifecycle_matrix_matches_authoritative_state_machine() -> None:
    from research_store.run_service import PERMITTED_TRANSITIONS, TERMINAL_STATES

    content = _skill_content()
    match = re.search(
        r"The authoritative PostgreSQL state machine permits these transitions:"
        r"\n\n```text\n(?P<body>.*?)\n```",
        content,
        re.DOTALL,
    )
    assert match is not None

    documented: dict[str, set[str]] = {}
    for line in match.group("body").splitlines():
        prior, targets = (part.strip() for part in line.split("→", 1))
        documented[prior] = {part.strip() for part in targets.split("|")}

    expected = {
        prior: set(targets)
        for prior, targets in PERMITTED_TRANSITIONS.items()
        if targets
    }
    assert documented == expected
    for state in TERMINAL_STATES:
        assert f"`{state}`" in content


@pytest.mark.parametrize(
    "argv",
    [
        [
            "lexical-search",
            "terms",
            "--run",
            RUN_ID,
            "--limit",
            "20",
            "--max-chars",
            "20000",
            "--max-tokens",
            "4000",
        ],
        [
            "pattern-search",
            "literal.identifier",
            "--mode",
            "literal",
            "--run",
            RUN_ID,
            "--limit",
            "20",
            "--max-chars",
            "20000",
            "--max-tokens",
            "4000",
        ],
    ],
)
def test_new_finspect_examples_parse(argv: list[str]) -> None:
    from research_store.inspection_cli import parser

    parsed = parser().parse_args(argv)
    assert parsed.command in {"lexical-search", "pattern-search"}


@pytest.mark.parametrize(
    "argv,expected_command",
    [
        (["run-verify", RUN_ID], "run-verify"),
        (["run-audit", RUN_ID], "run-audit"),
        (["audit-status", RUN_ID], "audit-status"),
        (["run-compare", RUN_ID, OTHER_RUN_ID], "run-compare"),
    ],
)
def test_documented_frun_backing_commands_parse(
    argv: list[str],
    expected_command: str,
) -> None:
    from research_store.cli import parser

    parsed = parser().parse_args(argv)
    assert parsed.command == expected_command


def test_skill_rejects_unbounded_stateful_checkpoint_retry() -> None:
    content = _skill_content()
    checkpoint_section = content.split(
        "A `fsearch_smart` exit status of `75`",
        1,
    )[1].split("## Stable replay", 1)[0]
    assert "while true" not in checkpoint_section
    assert "retry automatically" in checkpoint_section
    assert "create a replacement run" in checkpoint_section
    assert "unbounded retry loop" in checkpoint_section


def test_skill_describes_verify_as_blob_integrity_reporting_only() -> None:
    content = _skill_content()
    section = content.split(
        "## Blob-integrity reporting, audit scheduling, and comparison",
        1,
    )[1].split("## Qdrant and Valkey recovery", 1)[0]

    for required in (
        "invocation output `results`",
        "`snapshot` or `artifacts`",
        "It does not validate terminal state",
        "`total: 0` and exit status `0`",
        "it is not evidence that the run completed or passed",
    ):
        assert required in section

    assert "authoritative completion verification" not in content.split(
        "Use the run wrapper",
        1,
    )[0]
    assert "`frun verify` checks committed run evidence" not in content
    assert "verify or audit research runs" not in content


def test_verify_allows_zero_total_report_without_completion_evidence() -> None:
    from research_store.run_service import ResearchRunService

    run_uuid = UUID(int=1)

    class Runs:
        def list_invocations(self, run_id: UUID) -> list[dict]:
            assert run_id == run_uuid
            return []

    class UnitOfWork:
        runs = Runs()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    service = ResearchRunService(lambda: UnitOfWork(), blob_store=object())
    report = service.verify(run_uuid)

    assert report["target"] == str(run_uuid)
    assert report["total"] == 0
    assert report["available"] == 0
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0
    assert report["file_based_unverified"] == 0
    assert report["artifacts"] == []


def test_skill_describes_audit_as_partial_assessment_scheduling_only() -> None:
    content = _skill_content()
    section = content.split(
        "## Blob-integrity reporting, audit scheduling, and comparison",
        1,
    )[1].split("## Qdrant and Valkey recovery", 1)[0]

    for required in (
        "schedules and persists an audit assessment identity with status `partial`",
        "does not invoke a semantic provider",
        "execute deterministic audit-stage validation",
        "only a scheduled partial record",
        "are not consumed by the current scheduling path",
    ):
        assert required in section

    assert "persists an audit through the configured semantic authority" not in content
    assert "deterministic validation path" not in content


def test_trigger_audit_only_schedules_partial_assessment(monkeypatch) -> None:
    from research_store import container
    from research_store.run_service import ResearchRunService

    run_uuid = UUID(int=2)
    captured: dict[str, object] = {}

    class AuditService:
        def schedule_assessment(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {
                "assessment_id": "assessment-id",
                "status": kwargs["status"],
                "stages": kwargs["stage_set"],
            }

    monkeypatch.setattr(
        container,
        "build_audit_service",
        lambda uow_factory: AuditService(),
    )

    service = ResearchRunService(lambda: None)
    result = service.trigger_audit(
        run_uuid,
        target_hash="a" * 64,
        provider="openai",
        model="audit-model",
        force=True,
        stages=["rubric", "evidence"],
        max_calls=3,
        max_input_tokens=4096,
        fallback_provider="gemini",
        fallback_model="fallback-model",
    )

    assert captured["args"] == (run_uuid,)
    kwargs = captured["kwargs"]
    assert kwargs["target_type"] == "run"
    assert kwargs["target_id"] == run_uuid
    assert kwargs["status"] == "partial"
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "audit-model"
    assert kwargs["stage_set"] == ["rubric", "evidence"]
    for unused_option in (
        "force",
        "max_calls",
        "max_input_tokens",
        "fallback_provider",
        "fallback_model",
    ):
        assert unused_option not in kwargs
    assert result["status"] == "partial"
