"""Final installed-package and operator-boundary contracts for issue #269."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import tomllib
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"


def _run_python(source: str, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    paths = [str(SRC)]
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


def _build_wheel(wheel_dir: Path) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is not None:
        command = [
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
            "--python",
            sys.executable,
            str(ROOT),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(ROOT),
        ]
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_setuptools_installs_only_the_canonical_package_tree() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    package_dir = setuptools["package-dir"]

    assert "py-modules" not in setuptools
    assert "" not in package_dir
    assert package_dir["firecrawl_skill"] == "src/firecrawl_skill"
    assert all(
        package == "firecrawl_skill" or package.startswith("firecrawl_skill.")
        for package in setuptools["packages"]
    )


def test_source_imports_do_not_require_scripts_on_pythonpath() -> None:
    _run = _run_python(
        """
        import firecrawl_skill.research_domain
        import firecrawl_skill.research_store
        import firecrawl_skill.research_store.budget_policy
        import firecrawl_skill.research_store.retrieval
        import firecrawl_skill.research_store.retrieval.projection.indexing
        """
    )
    assert _run.returncode == 0, _run.stderr


def test_wheel_contains_only_canonical_runtime_modules(tmp_path: Path) -> None:
    result = _build_wheel(tmp_path)
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("firecrawl_skill-1.0.0-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(installed)

    required = {
        "firecrawl_skill/__init__.py",
        "firecrawl_skill/_data/budget-policy-v1.json",
        "firecrawl_skill/research_domain/__init__.py",
        "firecrawl_skill/research_domain/models.py",
        "firecrawl_skill/research_store/__init__.py",
        "firecrawl_skill/research_store/budget_policy.py",
        "firecrawl_skill/research_store/postgres.py",
        "firecrawl_skill/research_store/production_topology.py",
        "firecrawl_skill/research_store/acquisition/__init__.py",
        "firecrawl_skill/research_store/reporting/__init__.py",
        "firecrawl_skill/research_store/retrieval/__init__.py",
        "firecrawl_skill/research_store/retrieval/projection/indexing.py",
        "firecrawl_skill/research_store/alembic/versions/0044_terminal_provenance_guard.py",
        "firecrawl_skill/research_store/alembic/versions/0045_operator_actions.py",
        "firecrawl_skill/research_store/migrations/001_initial.sql",
    }
    assert required <= names

    forbidden = {
        "firecrawl_skill.research_store.budget_policy.py",
        "firecrawl_skill.research_store.acquisition.candidate_ranking.py",
        "firecrawl_skill.research_store.acquisition.classifier.py",
        "drain_index_jobs.py",
        "firecrawl_skill.model_gateway.py",
        "research_store/__init__.py",
        "research_domain/__init__.py",
        "firecrawl_skill/_compat.py",
    }
    assert forbidden.isdisjoint(names)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    isolated = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                from pathlib import Path

                from firecrawl_skill.research_store import budget_policy
                from firecrawl_skill.research_store.retrieval.projection.indexing import IndexWorker

                assert budget_policy.DEFAULT_POLICY
                assert Path(budget_policy.POLICY_PATH).is_file()
                assert IndexWorker.__module__ == (
                    "firecrawl_skill.research_store.retrieval.projection.indexing"
                )
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


def test_operator_entrypoints_target_canonical_modules() -> None:
    expected_fragments = {
        "fsearch": "-m firecrawl_skill.research_store.fsearch_cli",
        "fscrape": "-m firecrawl_skill.research_store.fscrape_cli",
        "research-db": "-m firecrawl_skill.research_store.cli",
        "frun": '"$SCRIPT_DIR/research-db" ingest-ready',
    }
    for name, fragment in expected_fragments.items():
        wrapper = (SCRIPTS / name).read_text(encoding="utf-8")
        assert fragment in wrapper


def test_research_db_help_preserves_operator_cli_behavior() -> None:
    env = os.environ.copy()
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"
    result = subprocess.run(
        [str(SCRIPTS / "research-db"), "--help"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: research-db" in result.stdout
    assert "Authoritative research asset store" in result.stdout


def test_alembic_path_and_current_head_remain_authoritative() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert (
        Path(script.dir).resolve()
        == (SRC / "firecrawl_skill" / "research_store" / "alembic").resolve()
    )
    assert script.get_heads() == ["0045_operator_actions"]
