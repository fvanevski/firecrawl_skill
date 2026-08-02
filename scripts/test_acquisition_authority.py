from __future__ import annotations

import os
import re
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.acquisition_authority import (
    AcquisitionPreflightError,
    execute_authoritative_acquisition,
    require_authoritative_acquisition,
)
from research_store.acquisition_service import FirecrawlSearchAdapter
from research_store.config import StoreConfig

_SCHEMA_HEAD = "0042_authoritative_acquisition"


class _FakeCursor:
    def __init__(
        self,
        *,
        schema_heads: tuple[str, ...] = (_SCHEMA_HEAD,),
        run_exists: bool = True,
        read_only: bool = False,
    ):
        self.schema_heads = schema_heads
        self.run_exists = run_exists
        self.read_only = read_only
        self.last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=None):
        self.last_sql = " ".join(sql.split())

    def fetchall(self):
        if "FROM alembic_version" not in self.last_sql:
            raise AssertionError(f"unexpected fetchall for {self.last_sql}")
        return [(head,) for head in self.schema_heads]

    def fetchone(self):
        if self.last_sql == "SHOW transaction_read_only":
            return ("on" if self.read_only else "off",)
        if "FROM research_runs WHERE id=" in self.last_sql:
            return (uuid4(),) if self.run_exists else None
        raise AssertionError(f"unexpected fetchone for {self.last_sql}")


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True


def _config(tmp_path: Path, database_url: str) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=database_url,
        blob_root=tmp_path / "blobs",
    )


def _connect_with(cursor: _FakeCursor):
    connection = _FakeConnection(cursor)
    return connection, lambda _database_url: connection


def _preflight(tmp_path: Path, cursor: _FakeCursor, *, run_id=None, dry_run=False):
    connection, connect_factory = _connect_with(cursor)
    context = require_authoritative_acquisition(
        run_id=run_id,
        dry_run=dry_run,
        config=_config(tmp_path, "postgresql://research@test/research"),
        connect_factory=connect_factory,
        expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
    )
    return context, connection


def test_acquisition_preflight_rejects_missing_database(tmp_path: Path):
    with pytest.raises(AcquisitionPreflightError, match="DATABASE_URL is required"):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, ""),
        )


def test_acquisition_preflight_rejects_non_postgresql_url(tmp_path: Path):
    with pytest.raises(
        AcquisitionPreflightError,
        match="DATABASE_URL must identify PostgreSQL",
    ):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "sqlite:///research.db"),
        )


def test_acquisition_preflight_rejects_schema_not_at_head(tmp_path: Path):
    _, connect_factory = _connect_with(_FakeCursor(schema_heads=("0041_old",)))
    with pytest.raises(AcquisitionPreflightError, match="not at Alembic head"):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


def test_acquisition_preflight_rejects_read_only_store(tmp_path: Path):
    _, connect_factory = _connect_with(_FakeCursor(read_only=True))
    with pytest.raises(AcquisitionPreflightError, match="read-only"):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


def test_acquisition_preflight_rejects_missing_run(tmp_path: Path):
    _, connect_factory = _connect_with(_FakeCursor(run_exists=False))
    run_id = uuid4()
    with pytest.raises(AcquisitionPreflightError, match=str(run_id)):
        require_authoritative_acquisition(
            run_id=run_id,
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


def test_acquisition_preflight_accepts_valid_run_and_writable_blob_root(
    tmp_path: Path,
):
    run_id = uuid4()
    context, connection = _preflight(
        tmp_path,
        _FakeCursor(),
        run_id=run_id,
    )

    assert context.run_id == run_id
    assert context.schema_heads == frozenset({_SCHEMA_HEAD})
    assert context.blob_root == tmp_path / "blobs"
    assert context.blob_root.is_dir()
    assert not list(context.blob_root.iterdir())
    assert connection.rolled_back is True


def test_acquisition_preflight_allows_dry_run_without_run(tmp_path: Path):
    context, _ = _preflight(
        tmp_path,
        _FakeCursor(run_exists=False),
        run_id=None,
        dry_run=True,
    )
    assert context.dry_run is True
    assert context.run_id is None


def test_failed_preflight_prevents_firecrawl_subprocess_invocation(tmp_path: Path):
    runner = mock.Mock()
    adapter = FirecrawlSearchAdapter(runner=runner)

    def invoke_firecrawl(_context):
        return adapter.search("must not run")

    with pytest.raises(AcquisitionPreflightError, match="DATABASE_URL is required"):
        execute_authoritative_acquisition(
            invoke_firecrawl,
            run_id=uuid4(),
            config=_config(tmp_path, ""),
        )

    runner.assert_not_called()


def test_acquisition_preflight_rejects_unwritable_blob_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from research_store import acquisition_authority

    _, connect_factory = _connect_with(_FakeCursor())
    monkeypatch.setattr(
        acquisition_authority.tempfile,
        "NamedTemporaryFile",
        mock.Mock(side_effect=OSError("permission denied")),
    )
    with pytest.raises(AcquisitionPreflightError, match="BLOB_ROOT is not writable"):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


_LITERAL_MARKERS = (
    "firecrawl_scratch",
    "SCRATCH_ROOT",
    "persist_results.py",
    "import-scratch",
)
_TOKEN_MARKERS = {
    "scratch_file": re.compile(r"(?<![A-Za-z0-9_])scratch_file(?![A-Za-z0-9_])"),
    "fread": re.compile(r"(?<![A-Za-z0-9_])fread(?![A-Za-z0-9_])"),
    "scratch-only persistence": re.compile(
        r"(?<![A-Za-z0-9_])scratch(?:-|_|\s+)only(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
}
_PATH_MARKERS = {
    "scripts/persist_results.py": "persist_results.py",
    "scripts/fread": "fread",
}
_EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "fixtures",
    "generated",
}

# Path-specific baseline for issue #184. Later scratch-removal issues must
# intentionally reduce this mapping as they delete each legacy surface.
_LEGACY_SURFACE_ALLOWLIST: dict[tuple[str, str], int] = {
    ("scripts/fread", "firecrawl_scratch"): 1,
    ("scripts/fread", "fread"): 1,
    ("scripts/fscrape", "firecrawl_scratch"): 2,
    ("scripts/fscrape", "fread"): 1,
    ("scripts/fscrape", "persist_results.py"): 1,
    ("scripts/fscrape", "scratch_file"): 3,
    ("scripts/fsearch", "firecrawl_scratch"): 1,
    ("scripts/fsearch", "fread"): 1,
    ("scripts/fsearch", "persist_results.py"): 1,
    ("scripts/fsearch", "scratch_file"): 7,
    ("scripts/fsearch_smart", "firecrawl_scratch"): 1,
    ("scripts/persist_results.py", "persist_results.py"): 1,
    ("scripts/persist_results.py", "scratch-only persistence"): 4,
    ("scripts/persist_results.py", "scratch_file"): 22,
    ("scripts/research_store/cli.py", "import-scratch"): 2,
    ("scripts/research_store/cli.py", "scratch_file"): 1,
    ("scripts/research_store/config.py", "SCRATCH_ROOT"): 1,
    ("scripts/research_store/config.py", "firecrawl_scratch"): 1,
    ("scripts/research_workflow.py", "scratch_file"): 1,
}


def _is_runtime_source(relative: Path) -> bool:
    if relative.name.startswith("test_"):
        return False
    if any(part in _EXCLUDED_PARTS for part in relative.parts):
        return False
    return relative.parts[:4] != (
        "scripts",
        "research_store",
        "alembic",
        "versions",
    )


def _legacy_surface_inventory(repo_root: Path) -> dict[tuple[str, str], int]:
    counts: Counter[tuple[str, str]] = Counter()
    scripts_root = repo_root / "scripts"

    for relative_path, marker in _PATH_MARKERS.items():
        if (repo_root / relative_path).is_file():
            counts[(relative_path, marker)] += 1

    for path in sorted(scripts_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root)
        if not _is_runtime_source(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative_text = relative.as_posix()
        for marker in _LITERAL_MARKERS:
            if relative_text == f"scripts/{marker}":
                continue
            count = text.count(marker)
            if count:
                counts[(relative_text, marker)] += count
        for marker, pattern in _TOKEN_MARKERS.items():
            if relative_text == f"scripts/{marker}":
                continue
            count = len(pattern.findall(text))
            if count:
                counts[(relative_text, marker)] += count
    return dict(sorted(counts.items()))


def test_legacy_surface_inventory_has_not_grown():
    actual = _legacy_surface_inventory(SCRIPTS.parent)
    assert actual == _LEGACY_SURFACE_ALLOWLIST, (
        "legacy scratch surface changed; update the path-specific allowlist only "
        f"for an intentional removal or approved migration:\nactual={actual!r}"
    )


def test_legacy_surface_inventory_excludes_tests_fixtures_and_generated_files(
    tmp_path: Path,
):
    repo = tmp_path
    (repo / "scripts" / "fixtures").mkdir(parents=True)
    (repo / "scripts" / "research_store" / "alembic" / "versions").mkdir(parents=True)
    (repo / "scripts" / "test_removed_compat.py").write_text(
        "SCRATCH_ROOT = 'removed'\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "fixtures" / "legacy.txt").write_text(
        "firecrawl_scratch\n",
        encoding="utf-8",
    )
    (
        repo / "scripts" / "research_store" / "alembic" / "versions" / "old.py"
    ).write_text(
        "scratch_file = 'historical'\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "generated").mkdir()
    (repo / "scripts" / "generated" / "manifest.json").write_text(
        '"import-scratch"\n',
        encoding="utf-8",
    )

    assert _legacy_surface_inventory(repo) == {}


def test_secure_ephemeral_temp_files_are_not_legacy_storage(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "atomic_export.py").write_text(
        "import tempfile\n"
        "with tempfile.NamedTemporaryFile(delete=True) as handle:\n"
        "    handle.write(b'ephemeral')\n",
        encoding="utf-8",
    )

    assert _legacy_surface_inventory(tmp_path) == {}


def test_authoritative_preflight_against_disposable_postgresql(tmp_path: Path):
    database_url = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("RESEARCH_STORE_TEST_DATABASE_URL is not configured")

    from research_store.postgres import (
        PostgresUnitOfWork,
        migrate,
        require_disposable_database_reset,
    )

    require_disposable_database_reset(
        database_url,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    migrate(database_url)
    config = _config(tmp_path, database_url)
    external_id = f"fr_{uuid4().hex}"
    with PostgresUnitOfWork(
        database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
    ) as uow:
        run_id = uow.start_run(
            "issue 184 authoritative acquisition preflight",
            {
                "external_run_id": external_id,
                "execution_mode": "autonomous_local",
                "metadata": {"test": "issue-184"},
            },
        )

    context = require_authoritative_acquisition(run_id=run_id, config=config)
    assert context.run_id == run_id
