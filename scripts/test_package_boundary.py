"""Regression coverage for the Phase 1 canonical package boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"


def _run_python(source: str, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    paths = [str(SRC), str(SCRIPTS)]
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_python_ok(source: str) -> None:
    result = _run_python(source)
    assert result.returncode == 0, result.stderr


def test_canonical_first_imports_share_legacy_module_identity() -> None:
    _assert_python_ok(
        """
        import importlib

        canonical_domain = importlib.import_module("firecrawl_skill.research_domain")
        canonical_models = importlib.import_module("firecrawl_skill.research_domain.models")
        canonical_store = importlib.import_module("firecrawl_skill.research_store")
        canonical_postgres = importlib.import_module("firecrawl_skill.research_store.postgres")

        legacy_domain = importlib.import_module("research_domain")
        legacy_models = importlib.import_module("research_domain.models")
        legacy_store = importlib.import_module("research_store")
        legacy_postgres = importlib.import_module("research_store.postgres")

        assert canonical_domain is legacy_domain
        assert canonical_models is legacy_models
        assert canonical_store is legacy_store
        assert canonical_postgres is legacy_postgres
        assert canonical_domain.HandoffPayload is legacy_domain.HandoffPayload
        assert canonical_store.ResearchRunService is legacy_store.ResearchRunService
        """
    )


def test_legacy_first_imports_share_canonical_module_identity() -> None:
    _assert_python_ok(
        """
        import importlib

        legacy_domain = importlib.import_module("research_domain")
        legacy_models = importlib.import_module("research_domain.models")
        legacy_store = importlib.import_module("research_store")
        legacy_postgres = importlib.import_module("research_store.postgres")

        canonical_domain = importlib.import_module("firecrawl_skill.research_domain")
        canonical_models = importlib.import_module("firecrawl_skill.research_domain.models")
        canonical_store = importlib.import_module("firecrawl_skill.research_store")
        canonical_postgres = importlib.import_module("firecrawl_skill.research_store.postgres")

        assert canonical_domain is legacy_domain
        assert canonical_models is legacy_models
        assert canonical_store is legacy_store
        assert canonical_postgres is legacy_postgres
        """
    )


def test_package_configuration_builds_canonical_and_compatibility_packages(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("firecrawl_skill-1.0.0-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    required = {
        "firecrawl_skill/__init__.py",
        "firecrawl_skill/_compat.py",
        "firecrawl_skill/research_store/__init__.py",
        "firecrawl_skill/research_domain/__init__.py",
        "research_store/__init__.py",
        "research_domain/__init__.py",
        "research_store/alembic/versions/0044_terminal_provenance_guard.py",
    }
    assert required <= names


def test_existing_operator_entrypoint_targets_are_unchanged() -> None:
    expected_fragments = {
        "fsearch": "-m research_store.fsearch_policy_service",
        "fscrape": "-m research_store.fscrape_cli",
        "research-db": "-m research_store.cli",
        "frun": '"$SCRIPT_DIR/research-db" ingest-ready',
    }
    for name, fragment in expected_fragments.items():
        wrapper = (SCRIPTS / name).read_text(encoding="utf-8")
        assert fragment in wrapper


def test_alembic_path_and_current_head_remain_authoritative() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert (
        Path(script.dir).resolve() == (SCRIPTS / "research_store" / "alembic").resolve()
    )
    assert script.get_heads() == ["0044_terminal_provenance_guard"]
