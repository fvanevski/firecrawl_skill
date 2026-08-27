from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "references" / "workflow-state-schema.md"


def test_controller_continuation_and_scope_fork_are_explicit() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "continue the same `fr_<uuid>` with `fresearch continue`" in text
    assert "Material scope change uses the durable controller fork/child-run boundary" in text
    assert "low-level reopen remains a specialist operation" in text.lower()
    assert "not the normal response to a completed controller run" in text


def test_machine_directives_replace_checkpoint_retry_choreography() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "`continue_automatic`" in text
    assert "`operator_action_required`" in text
    assert "do not invent recovery choreography" in text
    assert "checkpoint-recovery grammar" in text
    assert "not part of the current public smart surface" in text


def test_rtk_proxy_argv_is_not_shell_split() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "does **not** shell-split `argv[0]`" in text
    assert 'rtk proxy "<skill-root>/scripts/fresearch" run "<objective>"' in text
    assert (
        'rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64'
        in text
    )
    assert 'rtk proxy "python3 <skill-root>/scripts/drain_index_jobs.py"' in text
