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


def test_canonical_first_imports_preserve_canonical_module_identity() -> None:
    _assert_python_ok(
        """
        import importlib

        def assert_canonical(module, name):
            assert module.__name__ == name
            assert module.__spec__ is not None
            assert module.__spec__.name == name
            expected_package = name if hasattr(module, "__path__") else name.rpartition(".")[0]
            assert module.__package__ == expected_package

        canonical_domain = importlib.import_module("firecrawl_skill.research_domain")
        canonical_models = importlib.import_module("firecrawl_skill.research_domain.models")
        canonical_store = importlib.import_module("firecrawl_skill.research_store")
        canonical_postgres = importlib.import_module(
            "firecrawl_skill.research_store.postgres"
        )
        canonical_acquisition = importlib.import_module(
            "firecrawl_skill.research_store.acquisition"
        )
        canonical_acquisition_service = importlib.import_module(
            "firecrawl_skill.research_store.acquisition.service"
        )

        legacy_domain = importlib.import_module("research_domain")
        legacy_models = importlib.import_module("research_domain.models")
        legacy_store = importlib.import_module("research_store")
        legacy_postgres = importlib.import_module("research_store.postgres")
        legacy_acquisition = importlib.import_module("research_store.acquisition")
        legacy_acquisition_service = importlib.import_module(
            "research_store.acquisition.service"
        )

        assert canonical_domain is legacy_domain
        assert canonical_models is legacy_models
        assert canonical_store is legacy_store
        assert canonical_postgres is legacy_postgres
        assert canonical_acquisition is legacy_acquisition
        assert canonical_acquisition_service is legacy_acquisition_service
        assert canonical_domain.HandoffPayload is legacy_domain.HandoffPayload
        assert canonical_store.ResearchRunService is legacy_store.ResearchRunService

        sentinel = object()
        legacy_postgres._phase1_identity_probe = sentinel
        assert canonical_postgres._phase1_identity_probe is sentinel
        del canonical_postgres._phase1_identity_probe

        assert_canonical(canonical_domain, "firecrawl_skill.research_domain")
        assert_canonical(
            canonical_models, "firecrawl_skill.research_domain.models"
        )
        assert_canonical(canonical_store, "firecrawl_skill.research_store")
        assert_canonical(
            canonical_postgres, "firecrawl_skill.research_store.postgres"
        )
        assert_canonical(
            canonical_acquisition, "firecrawl_skill.research_store.acquisition"
        )
        assert_canonical(
            canonical_acquisition_service,
            "firecrawl_skill.research_store.acquisition.service",
        )
        """
    )


def test_legacy_first_imports_delegate_to_canonical_module_identity() -> None:
    _assert_python_ok(
        """
        import importlib

        def assert_canonical(module, name):
            assert module.__name__ == name
            assert module.__spec__ is not None
            assert module.__spec__.name == name
            expected_package = name if hasattr(module, "__path__") else name.rpartition(".")[0]
            assert module.__package__ == expected_package

        legacy_domain = importlib.import_module("research_domain")
        legacy_models = importlib.import_module("research_domain.models")
        legacy_store = importlib.import_module("research_store")
        legacy_postgres = importlib.import_module("research_store.postgres")
        legacy_acquisition = importlib.import_module("research_store.acquisition")
        legacy_acquisition_service = importlib.import_module(
            "research_store.acquisition.service"
        )

        canonical_domain = importlib.import_module("firecrawl_skill.research_domain")
        canonical_models = importlib.import_module("firecrawl_skill.research_domain.models")
        canonical_store = importlib.import_module("firecrawl_skill.research_store")
        canonical_postgres = importlib.import_module(
            "firecrawl_skill.research_store.postgres"
        )
        canonical_acquisition = importlib.import_module(
            "firecrawl_skill.research_store.acquisition"
        )
        canonical_acquisition_service = importlib.import_module(
            "firecrawl_skill.research_store.acquisition.service"
        )

        assert canonical_domain is legacy_domain
        assert canonical_models is legacy_models
        assert canonical_store is legacy_store
        assert canonical_postgres is legacy_postgres
        assert canonical_acquisition is legacy_acquisition
        assert canonical_acquisition_service is legacy_acquisition_service

        assert_canonical(legacy_domain, "firecrawl_skill.research_domain")
        assert_canonical(legacy_models, "firecrawl_skill.research_domain.models")
        assert_canonical(legacy_store, "firecrawl_skill.research_store")
        assert_canonical(
            legacy_postgres, "firecrawl_skill.research_store.postgres"
        )
        assert_canonical(
            legacy_acquisition, "firecrawl_skill.research_store.acquisition"
        )
        assert_canonical(
            legacy_acquisition_service,
            "firecrawl_skill.research_store.acquisition.service",
        )
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
        "firecrawl_skill/research_store/postgres.py",
        "firecrawl_skill/research_store/production_topology.py",
        "firecrawl_skill/research_store/acquisition/__init__.py",
        "firecrawl_skill/research_store/acquisition/authority.py",
        "firecrawl_skill/research_store/acquisition/models.py",
        "firecrawl_skill/research_store/acquisition/ports.py",
        "firecrawl_skill/research_store/acquisition/service.py",
        "firecrawl_skill/research_store/acquisition/direct_scrape.py",
        "firecrawl_skill/research_store/acquisition/direct_scrape_application.py",
        "firecrawl_skill/research_store/acquisition/adapters/__init__.py",
        "firecrawl_skill/research_store/acquisition/adapters/bounded_firecrawl.py",
        "firecrawl_skill/research_store/acquisition/adapters/firecrawl_search.py",
        "firecrawl_skill/research_store/acquisition/adapters/firecrawl_scrape.py",
        "firecrawl_skill/research_domain/__init__.py",
        "firecrawl_skill/research_domain/models.py",
        "research_store/__init__.py",
        "research_domain/__init__.py",
        "firecrawl_skill/research_store/alembic/versions/0044_terminal_provenance_guard.py",
        "firecrawl_skill/research_store/migrations/001_initial.sql",
    }
    assert required <= names
    assert "research_store/postgres.py" not in names
    assert "research_domain/models.py" not in names

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
                canonical_models = importlib.import_module(
                    "firecrawl_skill.research_domain.models"
                )
                canonical_store = importlib.import_module(
                    "firecrawl_skill.research_store"
                )
                canonical_postgres = importlib.import_module(
                    "firecrawl_skill.research_store.postgres"
                )
                canonical_production_topology = importlib.import_module(
                    "firecrawl_skill.research_store.production_topology"
                )
                canonical_acquisition = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition"
                )
                canonical_acquisition_service = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.service"
                )
                canonical_direct_scrape = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.direct_scrape"
                )
                canonical_direct_scrape_application = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.direct_scrape_application"
                )
                canonical_bounded_adapter = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl"
                )
                canonical_search_adapter = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.adapters.firecrawl_search"
                )
                canonical_scrape_adapter = importlib.import_module(
                    "firecrawl_skill.research_store.acquisition.adapters.firecrawl_scrape"
                )
                legacy_domain = importlib.import_module("research_domain")
                legacy_models = importlib.import_module("research_domain.models")
                legacy_store = importlib.import_module("research_store")
                legacy_postgres = importlib.import_module("research_store.postgres")
                legacy_acquisition = importlib.import_module("research_store.acquisition")
                legacy_acquisition_service = importlib.import_module(
                    "research_store.acquisition.service"
                )

                assert canonical_domain is legacy_domain
                assert canonical_models is legacy_models
                assert canonical_store is legacy_store
                assert canonical_postgres is legacy_postgres
                assert canonical_acquisition is legacy_acquisition
                assert canonical_acquisition_service is legacy_acquisition_service

                assert canonical_domain.__name__ == "firecrawl_skill.research_domain"
                assert canonical_domain.__spec__.name == "firecrawl_skill.research_domain"
                assert canonical_models.__name__ == (
                    "firecrawl_skill.research_domain.models"
                )
                assert canonical_models.__spec__.name == (
                    "firecrawl_skill.research_domain.models"
                )
                assert canonical_store.__name__ == "firecrawl_skill.research_store"
                assert canonical_store.__spec__.name == "firecrawl_skill.research_store"
                assert canonical_postgres.__name__ == (
                    "firecrawl_skill.research_store.postgres"
                )
                assert canonical_postgres.__spec__.name == (
                    "firecrawl_skill.research_store.postgres"
                )
                assert canonical_acquisition.__name__ == (
                    "firecrawl_skill.research_store.acquisition"
                )
                assert canonical_acquisition_service.__name__ == (
                    "firecrawl_skill.research_store.acquisition.service"
                )
                assert (
                    canonical_direct_scrape.DirectScrapeService
                    is canonical_direct_scrape_application.DirectScrapeService
                )
                assert canonical_production_topology.ProductionBoundedExtractionStage
                assert canonical_bounded_adapter.BoundedFirecrawlSearchAdapter
                assert canonical_search_adapter.MetadataOnlyFirecrawlSearchAdapter
                assert canonical_scrape_adapter.FirecrawlDirectScrapeAdapter
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


def test_frun_requires_authoritative_database_before_dispatch() -> None:
    env = os.environ.copy()
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [str(SCRIPTS / "frun"), "status", "fr_test"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "ERROR: frun requires the authoritative PostgreSQL store" in result.stderr


def test_frun_status_dispatches_through_research_db_without_mutation(
    tmp_path: Path,
) -> None:
    frun = tmp_path / "frun"
    frun.write_bytes((SCRIPTS / "frun").read_bytes())
    frun.chmod(0o755)

    call_log = tmp_path / "research-db-calls.txt"
    research_db = tmp_path / "research-db"
    research_db.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALL_LOG"
case "${1:-}" in
  ingest-ready)
    exit 0
    ;;
  run-status)
    printf '{"state":"created"}\\n'
    exit 0
    ;;
  *)
    echo "unexpected research-db call: $*" >&2
    exit 99
    ;;
esac
""",
        encoding="utf-8",
    )
    research_db.chmod(0o755)

    env = os.environ.copy()
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"
    env["DATABASE_URL"] = "postgresql://package-boundary.invalid/review"
    env["CALL_LOG"] = str(call_log)
    result = subprocess.run(
        [str(frun), "status", "fr_test"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"state":"created"}\n'
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "ingest-ready",
        "run-status fr_test",
    ]


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
