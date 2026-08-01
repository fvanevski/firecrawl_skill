from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

SOURCE_ADAPTER = pathlib.Path(__file__).with_name("research-env")


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
        "qwen3-embedding-0.6b-q6_k@8c605f43dcb0",
        "loaded from root",
    ]


def test_explicit_environment_wins_over_root_env(tmp_path: pathlib.Path) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text(
        'EMBEDDING_MODEL="file-model"\n'
        'EMBEDDING_REVISION="file-revision"\n',
        encoding="utf-8",
    )
    environment["EMBEDDING_MODEL"] = "explicit-model"

    values = _source_values(root, adapter, environment)

    assert values == ["explicit-model", "file-revision", ""]


def test_defaults_apply_when_root_env_is_absent(tmp_path: pathlib.Path) -> None:
    root, adapter, environment = _sandbox(tmp_path)

    values = _source_values(root, adapter, environment)

    assert values == [
        "embed",
        "qwen3-embedding-0.6b-q6_k@8c605f43dcb0",
        "",
    ]


def test_loading_env_preserves_caller_allexport_option(
    tmp_path: pathlib.Path,
) -> None:
    root, adapter, environment = _sandbox(tmp_path)
    (root / ".env").write_text('EMBEDDING_MODEL="file-model"\n', encoding="utf-8")

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
