from __future__ import annotations

import os
import shutil
import subprocess
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ENV = ROOT / "scripts" / "research-env"
EXPECTED_PACKAGE_ROOT = (ROOT / "src" / "firecrawl_skill").resolve()


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "postgresql://example.invalid/db",
            "QDRANT_API_KEY": "test",
            "QDRANT_URL": "http://example.invalid",
            "VALKEY_URL": "redis://example.invalid/0",
            "EMBEDDING_MODEL": "test-embedding",
            "EMBEDDING_REVISION": "test-revision",
            "EMBEDDING_DIMENSION": "8",
        }
    )
    return env


def _fake_python(tmp_path: Path, package_root: Path) -> Path:
    executable = tmp_path / "python"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ ${1:-} == -c ]]; then\n"
        f"  printf '%s\\n' '{package_root}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _source(
    env: dict[str, str], *, cwd: Path | None = None, adapter: Path = RESEARCH_ENV
) -> subprocess.CompletedProcess[str]:
    command = (
        'source "$1"; printf "python=%s\\nruntime=%s\\n" '
        '"$FIRECRAWL_RESEARCH_PYTHON" "$FIRECRAWL_RESEARCH_RUNTIME_ROOT"'
    )
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command, "bash", str(adapter)],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_explicit_interpreter_with_matching_source_is_accepted(tmp_path: Path) -> None:
    env = _base_env()
    env["FIRECRAWL_RESEARCH_PYTHON"] = str(
        _fake_python(tmp_path, EXPECTED_PACKAGE_ROOT)
    )
    result = _source(env)
    assert result.returncode == 0, result.stderr
    assert f"runtime={EXPECTED_PACKAGE_ROOT}" in result.stdout


def test_explicit_interpreter_path_with_spaces_is_accepted(tmp_path: Path) -> None:
    interpreter_dir = tmp_path / "python path with spaces"
    interpreter_dir.mkdir()
    env = _base_env()
    env["FIRECRAWL_RESEARCH_PYTHON"] = str(
        _fake_python(interpreter_dir, EXPECTED_PACKAGE_ROOT)
    )
    result = _source(env)
    assert result.returncode == 0, result.stderr
    assert f"runtime={EXPECTED_PACKAGE_ROOT}" in result.stdout


def test_runtime_provenance_is_cwd_independent(tmp_path: Path) -> None:
    env = _base_env()
    env["FIRECRAWL_RESEARCH_PYTHON"] = str(
        _fake_python(tmp_path, EXPECTED_PACKAGE_ROOT)
    )
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    result = _source(env, cwd=cwd)
    assert result.returncode == 0, result.stderr


def test_explicit_interpreter_importing_another_checkout_fails_closed(
    tmp_path: Path,
) -> None:
    other = tmp_path / "other" / "src" / "firecrawl_skill"
    other.mkdir(parents=True)
    env = _base_env()
    env["FIRECRAWL_RESEARCH_PYTHON"] = str(_fake_python(tmp_path, other))
    result = _source(env)
    assert result.returncode != 0
    assert "research runtime provenance mismatch" in result.stderr
    assert "selected interpreter:" in result.stderr
    assert f"expected source root: {EXPECTED_PACKAGE_ROOT}" in result.stderr
    assert f"actual imported package root: {other}" in result.stderr


def test_missing_explicit_interpreter_fails_before_runtime_use(tmp_path: Path) -> None:
    env = _base_env()
    env["FIRECRAWL_RESEARCH_PYTHON"] = str(tmp_path / "missing-python")
    result = _source(env)
    assert result.returncode != 0
    assert "FIRECRAWL_RESEARCH_PYTHON is not executable" in result.stderr


def test_canonical_local_venv_is_selected_and_imports_exact_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "skill"
    scripts = checkout / "scripts"
    package_root = checkout / "src" / "firecrawl_skill"
    scripts.mkdir(parents=True)
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    adapter = scripts / "research-env"
    shutil.copy2(RESEARCH_ENV, adapter)

    venv_root = checkout / ".venv-research-store"
    venv.EnvBuilder(with_pip=False).create(venv_root)
    interpreter = venv_root / "bin" / "python"
    site_result = subprocess.run(
        [str(interpreter), "-c", "import site; print(site.getsitepackages()[0])"],
        check=True,
        capture_output=True,
        text=True,
    )
    site_packages = Path(site_result.stdout.strip())
    (site_packages / "firecrawl-review-checkout.pth").write_text(
        str((checkout / "src").resolve()) + "\n", encoding="utf-8"
    )

    env = _base_env()
    env.pop("FIRECRAWL_RESEARCH_PYTHON", None)
    env.update(
        {
            "RESEARCH_POSTGRES_DIR": str(tmp_path / "missing-postgres"),
            "RESEARCH_QDRANT_DIR": str(tmp_path / "missing-qdrant"),
            "RESEARCH_VALKEY_DIR": str(tmp_path / "missing-valkey"),
        }
    )
    result = _source(env, cwd=checkout, adapter=adapter)

    assert result.returncode == 0, result.stderr
    assert f"python={interpreter}" in result.stdout
    assert f"runtime={package_root.resolve()}" in result.stdout
