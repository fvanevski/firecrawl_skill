"""Installed-package regressions for the issue #263 retrieval/projection slice."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_REQUIRED_RETRIEVAL_FILES = {
    "firecrawl_skill/research_store/retrieval/__init__.py",
    "firecrawl_skill/research_store/retrieval/ranking.py",
    "firecrawl_skill/research_store/retrieval/service.py",
    "firecrawl_skill/research_store/retrieval/postgres.py",
    "firecrawl_skill/research_store/retrieval/projection/__init__.py",
    "firecrawl_skill/research_store/retrieval/projection/authority.py",
    "firecrawl_skill/research_store/retrieval/projection/qdrant.py",
    "firecrawl_skill/research_store/retrieval/projection/reconciliation.py",
    "firecrawl_skill/research_store/retrieval/projection/postgres_jobs.py",
    "firecrawl_skill/research_store/retrieval/projection/indexing.py",
    "firecrawl_skill/research_store/retrieval/projection/checkpoint_indexing_stage.py",
    "firecrawl_skill/research_store/retrieval/projection/index_checkpoint_service.py",
}


def test_retrieval_projection_packages_build_and_import_in_isolation(tmp_path: Path) -> None:
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
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert _REQUIRED_RETRIEVAL_FILES <= names
        archive.extractall(installed)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    isolated = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib

                retrieval = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval"
                )
                ranking = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.ranking"
                )
                service = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.service"
                )
                postgres = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.postgres"
                )
                projection = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.projection"
                )
                qdrant = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.projection.qdrant"
                )
                authority = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.projection.authority"
                )
                checkpoint = importlib.import_module(
                    "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_service"
                )

                legacy_retrieval = importlib.import_module("research_store.retrieval")
                legacy_service = importlib.import_module("research_store.retrieval_service")
                legacy_qdrant = importlib.import_module("research_store.qdrant")
                legacy_authority = importlib.import_module("research_store.qdrant_authority")

                assert hasattr(retrieval, "__path__")
                assert retrieval is legacy_retrieval
                assert service is legacy_service
                assert qdrant is legacy_qdrant
                assert authority is legacy_authority

                assert retrieval.__name__ == "firecrawl_skill.research_store.retrieval"
                assert ranking.CohereCompatibleReranker.__module__ == (
                    "firecrawl_skill.research_store.retrieval.ranking"
                )
                assert service.RetrievalService.__module__ == (
                    "firecrawl_skill.research_store.retrieval.service"
                )
                assert postgres.PostgresRetrievalRepository.__module__ == (
                    "firecrawl_skill.research_store.retrieval.postgres"
                )
                assert qdrant.QdrantIndex.__module__ == (
                    "firecrawl_skill.research_store.retrieval.projection.qdrant"
                )
                assert projection.__doc__ and "non-authoritative" in projection.__doc__
                assert checkpoint.IndexCheckpointService.__module__.endswith(
                    ".research_store.index_checkpoint_service"
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
