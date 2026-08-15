"""Regression coverage for the Phase 1 canonical package boundary."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import tomllib
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


def _absolute_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _research_store_support_module_closure() -> set[str]:
    top_level_modules = {
        path.stem: path for path in SCRIPTS.glob("*.py") if path.name != "__init__.py"
    }
    pending = list((SCRIPTS / "research_store").rglob("*.py"))
    inspected: set[Path] = set()
    required: set[str] = set()

    while pending:
        path = pending.pop()
        if path in inspected:
            continue
        inspected.add(path)
        for root in _absolute_import_roots(path):
            support_path = top_level_modules.get(root)
            if support_path is None or root in required:
                continue
            required.add(root)
            pending.append(support_path)

    return required


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


def test_package_configuration_matches_support_module_closure() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    configured = set(config["tool"]["setuptools"]["py-modules"])
    required = _research_store_support_module_closure()

    assert configured == required, (
        f"py-modules must equal the research_store support-module closure; "
        f"missing={sorted(required - configured)}, "
        f"extra={sorted(configured - required)}"
    )


def test_package_configuration_builds_and_runs_without_repository_path(
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
    wheel = wheels[0]
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(installed)

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    support_modules = set(config["tool"]["setuptools"]["py-modules"])
    required = {
        *(f"{module}.py" for module in support_modules),
        "firecrawl_skill/__init__.py",
        "firecrawl_skill/_compat.py",
        "firecrawl_skill/_data/budget-policy-v1.json",
        "firecrawl_skill/research_store/__init__.py",
        "firecrawl_skill/research_domain/__init__.py",
        "research_store/__init__.py",
        "research_domain/__init__.py",
        "research_store/alembic/versions/0044_terminal_provenance_guard.py",
        "research_store/migrations/001_initial.sql",
    }
    assert required <= names

    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    isolated = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib
                from pathlib import Path

                import budget_policy

                canonical_domain = importlib.import_module(
                    "firecrawl_skill.research_domain"
                )
                canonical_store = importlib.import_module(
                    "firecrawl_skill.research_store"
                )
                legacy_domain = importlib.import_module("research_domain")
                legacy_store = importlib.import_module("research_store")

                assert canonical_domain is legacy_domain
                assert canonical_store is legacy_store
                assert Path(budget_policy.POLICY_PATH).is_file()
                """
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr


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
