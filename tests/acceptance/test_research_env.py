from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from firecrawl_skill.research_store.config import StoreConfig

SOURCE_ADAPTER = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "research-env"
)


def _sandbox(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, dict[str, str]]:
    root = tmp_path / "skill"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    adapter = scripts / "research-env"
    shutil.copy2(SOURCE_ADAPTER, adapter)

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(temp_dir),
        "RESEARCH_POSTGRES_DIR": str(tmp_path / "missing-postgres"),
        "RESEARCH_QDRANT_DIR": str(tmp_path / "missing-qdrant"),
        "RESEARCH_VALKEY_DIR": str(tmp_path / "missing-valkey"),
    }
    return root, adapter, environment


def _source_values(
    root: pathlib.Path,
    adapter: pathlib.Path,
    environment: dict[str, str],
) -> list[str]:
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            """
set -euo pipefail
source "$1"
printf '%s\n' \
  "$EMBEDDING_MODEL" \
  "$EMBEDDING_REVISION" \
  "$EMBEDDING_DIMENSION" \
  "${CUSTOM_FROM_ENV_FILE:-}"
""",
            "bash",
            str(adapter),
        ],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_loads_root_env_and_ignores_legacy_nested_path(
    tmp_path: pathlib.Path,
) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text(
        'EMBEDDING_MODEL="root-model"\n'
        'EMBEDDING_REVISION="root-revision"\n'
        'EMBEDDING_DIMENSION="384"\n'
        'CUSTOM_FROM_ENV_FILE="loaded from root"\n',
        encoding="utf-8",
    )
    legacy_dir = root / "firecrawl"
    legacy_dir.mkdir()
    (legacy_dir / ".env").write_text(
        'EMBEDDING_MODEL="legacy-nested-model"\n',
        encoding="utf-8",
    )

    values = _source_values(root, adapter, environment)

    assert values == [
        "root-model",
        "root-revision",
        "384",
        "loaded from root",
    ]


def test_explicit_environment_wins_over_root_env(tmp_path: pathlib.Path) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text(
        'EMBEDDING_MODEL="file-model"\n'
        'EMBEDDING_REVISION="file-revision"\n'
        'EMBEDDING_DIMENSION="512"\n',
        encoding="utf-8",
    )
    environment["EMBEDDING_MODEL"] = "explicit-model"

    values = _source_values(root, adapter, environment)

    assert values == ["explicit-model", "file-revision", "512", ""]


def test_missing_embedding_identity_fails_closed(tmp_path: pathlib.Path) -> None:
    root, adapter, environment = _sandbox(tmp_path)

    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'set -euo pipefail; source "$1"',
            "bash",
            str(adapter),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "EMBEDDING_MODEL must be set explicitly or in" in result.stderr


def test_loading_env_preserves_caller_allexport_option(
    tmp_path: pathlib.Path,
) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text(
        'EMBEDDING_MODEL="file-model"\n'
        'EMBEDDING_REVISION="file-revision"\n'
        'EMBEDDING_DIMENSION="256"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            """
set -euo pipefail
set -a
source "$1"
case $- in
  *a*) printf 'enabled\n' ;;
  *) printf 'disabled\n' ;;
esac
""",
            "bash",
            str(adapter),
        ],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "enabled\n"


def test_malformed_root_env_fails_closed(tmp_path: pathlib.Path) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text(
        'EMBEDDING_MODEL="partial-value"\nfalse\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'set -euo pipefail; source "$1"',
            "bash",
            str(adapter),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unable to load" in result.stderr


def test_store_config_uses_resolved_embedding_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "configured-model")
    monkeypatch.setenv("EMBEDDING_REVISION", "configured-revision")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "640")

    config = StoreConfig.from_env()

    assert config.embedding_model == "configured-model"
    assert config.embedding_revision == "configured-revision"
    assert config.embedding_dimension == 640


@pytest.mark.parametrize(
    "missing_name",
    ("EMBEDDING_MODEL", "EMBEDDING_REVISION", "EMBEDDING_DIMENSION"),
)
def test_store_config_requires_embedding_identity(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "configured-model")
    monkeypatch.setenv("EMBEDDING_REVISION", "configured-revision")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "640")
    monkeypatch.delenv(missing_name)

    with pytest.raises(ValueError, match=missing_name):
        StoreConfig.from_env()
