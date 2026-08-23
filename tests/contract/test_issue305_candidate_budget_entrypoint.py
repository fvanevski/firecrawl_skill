from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "scripts" / "candidate_budget_cli.py"
WRAPPER = ROOT / "scripts" / "candidate-budget"


def test_raw_candidate_budget_entrypoint_fails_with_wrapper_guidance() -> None:
    env = os.environ.copy()
    env.pop("FIRECRAWL_CANDIDATE_BUDGET_WRAPPER", None)
    result = subprocess.run(
        [sys.executable, str(RAW), "config", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "use scripts/candidate-budget" in result.stderr
    assert "research-env provenance" in result.stderr


def test_wrapper_binds_research_env_before_internal_entrypoint() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'source "$script_dir/research-env"' in text
    assert "FIRECRAWL_CANDIDATE_BUDGET_WRAPPER=1" in text
    assert 'exec "$research_python" "$script_dir/candidate_budget_cli.py"' in text
