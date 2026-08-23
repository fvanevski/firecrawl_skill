from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "references" / "workflow-state-schema.md"


def test_resume_reopen_new_run_decision_is_explicit():
    text = GUIDE.read_text(encoding="utf-8")
    assert "Same objective and same authoritative time window, nonterminal run" in text
    assert "resume the same run from its persisted state" in text
    assert "Same objective/window, terminal run" in text
    assert "explicitly `frun reopen <fr_id>`" in text
    assert "Materially changed research objective or authoritative time window" in text
    assert "create a new run" in text


def test_checkpoint_75_is_deliberate_and_not_an_unbounded_retry():
    text = GUIDE.read_text(encoding="utf-8")
    assert "exit status of `75` is a deliberate resumable checkpoint" in text
    assert "resume that same run as a separate action" in text
    assert "Never place `fsearch_smart`, `frun resume`, `frun seal-acquisition`" in text
    assert "unbounded retry loop" in text


def test_rtk_proxy_argv_is_not_shell_split():
    text = GUIDE.read_text(encoding="utf-8")
    assert "does **not** shell-split `argv[0]`" in text
    assert 'rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"' in text
    assert (
        'rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64'
        in text
    )
    assert 'rtk proxy "python3 <skill-root>/scripts/drain_index_jobs.py"' in text
