from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from psycopg import sql

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter as FirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.acquisition.authority import (
    ACQUISITION_ENTRY_STATES,
    ACQUISITION_TABLE_PRIVILEGES,
    AcquisitionPreflightError,
    require_authoritative_acquisition,
)
from firecrawl_skill.research_store.acquisition.ports import SearchAdapter
from firecrawl_skill.research_store.acquisition.service import AcquisitionService
from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult, utcnow
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.run_service import TERMINAL_STATES

_SCHEMA_HEAD = "0042_authoritative_acquisition"
TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


class _FakeCursor:
    def __init__(
        self,
        *,
        schema_heads: tuple[str, ...] = (_SCHEMA_HEAD,),
        run_exists: bool = True,
        run_state: str = "acquiring",
        lifecycle_revision: int = 7,
        read_only: bool = False,
        denied_privileges: frozenset[tuple[str, str]] = frozenset(),
    ):
        self.schema_heads = schema_heads
        self.run_exists = run_exists
        self.run_state = run_state
        self.lifecycle_revision = lifecycle_revision
        self.read_only = read_only
        self.denied_privileges = denied_privileges
        self.last_sql = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql_text, params=None):
        self.last_sql = " ".join(sql_text.split())
        self.last_params = params

    def fetchall(self):
        if "FROM alembic_version" not in self.last_sql:
            raise AssertionError(f"unexpected fetchall for {self.last_sql}")
        return [(head,) for head in self.schema_heads]

    def fetchone(self):
        if self.last_sql == "SHOW transaction_read_only":
            return ("on" if self.read_only else "off",)
        if "has_table_privilege" in self.last_sql:
            assert self.last_params is not None
            table, privilege = self.last_params
            return ((table, privilege) not in self.denied_privileges,)
        if "FROM research_runs WHERE id=" in self.last_sql:
            if not self.run_exists:
                return None
            return (uuid4(), self.run_state, self.lifecycle_revision)
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
        config=_config(tmp_path, "postgresql://research:secret@test/research"),
        connect_factory=connect_factory,
        expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
    )
    return context, connection


def _guarded_search(
    adapter: FirecrawlSearchAdapter,
    *,
    run_id: UUID | str | None,
    config: StoreConfig,
    connect_factory=connect,
    expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
):
    preflight_kwargs = {
        "run_id": run_id,
        "config": config,
        "connect_factory": connect_factory,
    }
    if expected_heads_factory is not None:
        preflight_kwargs["expected_heads_factory"] = expected_heads_factory
    context = require_authoritative_acquisition(**preflight_kwargs)
    return context, adapter.search("guarded acquisition")


def test_public_api_does_not_expose_generic_acquisition_callback():
    from firecrawl_skill import research_store

    assert not hasattr(research_store, "execute_authoritative_acquisition")


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


def test_acquisition_preflight_rejects_incomplete_acquisition_privileges(
    tmp_path: Path,
):
    _, connect_factory = _connect_with(
        _FakeCursor(denied_privileges=frozenset({("research_events", "INSERT")}))
    )
    with pytest.raises(AcquisitionPreflightError, match="research_events:INSERT"):
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


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES))
def test_acquisition_preflight_rejects_terminal_run(
    tmp_path: Path,
    terminal_state: str,
):
    _, connect_factory = _connect_with(_FakeCursor(run_state=terminal_state))
    terminal_pattern = rf"terminal \({terminal_state}\)"
    with pytest.raises(AcquisitionPreflightError, match=terminal_pattern):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


@pytest.mark.parametrize(
    "ineligible_state",
    sorted(
        {
            "extracting",
            "indexing",
            "retrieving",
            "synthesizing",
            "validating",
        }
    ),
)
def test_acquisition_preflight_rejects_non_terminal_ineligible_state(
    tmp_path: Path,
    ineligible_state: str,
):
    assert ineligible_state not in ACQUISITION_ENTRY_STATES
    _, connect_factory = _connect_with(_FakeCursor(run_state=ineligible_state))
    with pytest.raises(AcquisitionPreflightError, match="not acquisition-eligible"):
        require_authoritative_acquisition(
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
            expected_heads_factory=lambda: frozenset({_SCHEMA_HEAD}),
        )


def test_acquisition_preflight_returns_run_revision_without_leaking_credentials(
    tmp_path: Path,
):
    run_id = uuid4()
    context, connection = _preflight(
        tmp_path,
        _FakeCursor(run_state="acquiring", lifecycle_revision=11),
        run_id=run_id,
    )

    assert context.run_id == run_id
    assert context.run_state == "acquiring"
    assert context.lifecycle_revision == 11
    assert context.schema_heads == frozenset({_SCHEMA_HEAD})
    assert context.blob_root == tmp_path / "blobs"
    assert not context.blob_root.exists()
    assert "secret" not in repr(context)
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
    assert context.run_state is None
    assert context.lifecycle_revision is None


@pytest.mark.parametrize(
    "case",
    (
        "missing_database",
        "invalid_database",
        "unreachable_database",
        "schema_mismatch",
        "read_only",
        "missing_privilege",
        "missing_run",
        "terminal_run",
        "ineligible_run",
    ),
)
def test_every_database_preflight_failure_prevents_network(
    tmp_path: Path,
    case: str,
):
    runner = mock.Mock()
    adapter = FirecrawlSearchAdapter(runner=runner)
    config = _config(tmp_path, "postgresql://research@test/research")
    cursor = _FakeCursor()
    expected_heads = lambda: frozenset({_SCHEMA_HEAD})

    if case == "missing_database":
        config = _config(tmp_path, "")
    elif case == "invalid_database":
        config = _config(tmp_path, "sqlite:///research.db")
    elif case == "unreachable_database":
        connect_factory = mock.Mock(side_effect=OSError("connection refused"))
        with pytest.raises(AcquisitionPreflightError, match="connection refused"):
            _guarded_search(
                adapter,
                run_id=uuid4(),
                config=config,
                connect_factory=connect_factory,
                expected_heads_factory=expected_heads,
            )
        runner.assert_not_called()
        return
    elif case == "schema_mismatch":
        cursor = _FakeCursor(schema_heads=("0041_old",))
    elif case == "read_only":
        cursor = _FakeCursor(read_only=True)
    elif case == "missing_privilege":
        cursor = _FakeCursor(
            denied_privileges=frozenset({("search_responses", "INSERT")})
        )
    elif case == "missing_run":
        cursor = _FakeCursor(run_exists=False)
    elif case == "terminal_run":
        cursor = _FakeCursor(run_state="completed")
    elif case == "ineligible_run":
        cursor = _FakeCursor(run_state="synthesizing")

    _, connect_factory = _connect_with(cursor)
    with pytest.raises(AcquisitionPreflightError):
        _guarded_search(
            adapter,
            run_id=uuid4(),
            config=config,
            connect_factory=connect_factory,
            expected_heads_factory=expected_heads,
        )
    runner.assert_not_called()


def test_unwritable_blob_root_prevents_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import firecrawl_skill.research_store.acquisition.authority as acquisition_authority

    runner = mock.Mock()
    adapter = FirecrawlSearchAdapter(runner=runner)
    _, connect_factory = _connect_with(_FakeCursor())
    monkeypatch.setattr(
        acquisition_authority.tempfile,
        "NamedTemporaryFile",
        mock.Mock(side_effect=OSError("permission denied")),
    )

    with pytest.raises(AcquisitionPreflightError, match="not durably writable"):
        _guarded_search(
            adapter,
            run_id=uuid4(),
            config=_config(tmp_path, "postgresql://research@test/research"),
            connect_factory=connect_factory,
        )
    runner.assert_not_called()
    assert not (tmp_path / "blobs").exists()


def test_blob_probe_fsyncs_file_and_directory_and_removes_probe_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import firecrawl_skill.research_store.acquisition.authority as acquisition_authority

    fsync_calls: list[int] = []
    real_fsync = acquisition_authority.os.fsync

    def recording_fsync(descriptor: int):
        fsync_calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(acquisition_authority.os, "fsync", recording_fsync)
    context, _ = _preflight(tmp_path, _FakeCursor(), run_id=uuid4())

    assert len(fsync_calls) >= 3
    assert not context.blob_root.exists()


def test_rc6_removes_legacy_runtime_surfaces(monkeypatch: pytest.MonkeyPatch):
    from dataclasses import fields
    from inspect import signature

    from firecrawl_skill.research_store.acquisition.service import AcquisitionResult
    from firecrawl_skill.research_store.cli import parser

    for removed_path in (
        SCRIPTS / "persist_results.py",
        SCRIPTS / "fread",
    ):
        assert not removed_path.exists()

    monkeypatch.setenv("SCRATCH_ROOT", "/tmp/must-not-be-read")
    config = StoreConfig.from_env()
    assert "scratch_root" not in StoreConfig.__dataclass_fields__
    assert not hasattr(config, "scratch_root")

    result_fields = {field.name for field in fields(AcquisitionResult)}
    assert {"scratch_exported", "scratch_error"}.isdisjoint(result_fields)

    search_parameters = signature(AcquisitionService.execute_search).parameters
    assert {"scratch_dir", "export_scratch"}.isdisjoint(search_parameters)

    root = parser()
    subparsers = next(
        action
        for action in root._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "import-scratch" not in subparsers.choices

    acquisition_parser = subparsers.choices["acquisition-search"]
    option_strings = {
        option
        for action in acquisition_parser._actions
        for option in action.option_strings
    }
    assert "--scratch-dir" not in option_strings


_LITERAL_MARKERS = (
    "firecrawl_scratch",
    "SCRATCH_ROOT",
    "persist_results.py",
    "import-scratch",
    "_corpus.json",
    "_search.json",
    "_meta.json",
    "_context.json",
    "_candidates.json",
    "_index.md",
    "_workflow_input.json",
    "--reuse-search",
    "FIRECRAWL_RESEARCH_ACTIVE",
    "FIRECRAWL_CAPTURE_RAW",
    "FIRECRAWL_RESEARCH_PERSIST",
)
_TOKEN_MARKERS = {
    "scratch_file": re.compile(r"(?<![A-Za-z0-9_])scratch_file(?![A-Za-z0-9_])"),
    "raw_scratch_file": re.compile(
        r"(?<![A-Za-z0-9_])raw_scratch_file(?![A-Za-z0-9_])"
    ),
    "scratch_dir": re.compile(r"(?<![A-Za-z0-9_])scratch_dir(?![A-Za-z0-9_])"),
    "fread": re.compile(r"(?<![A-Za-z0-9_])fread(?![A-Za-z0-9_])"),
    "scratch-only persistence": re.compile(
        r"(?<![A-Za-z0-9_])scratch(?:-|_|\s+)only(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    "reuse_search": re.compile(r"(?<![A-Za-z0-9_])reuse_search(?![A-Za-z0-9_])"),
    "scrape_ranks": re.compile(r"(?<![A-Za-z0-9_])scrape_ranks(?![A-Za-z0-9_])"),
    "legacy _raw acquisition directory": re.compile(
        r"(?:[furbFURB]{0,2})?[\"']_raw(?:/|[\"'])"
    ),
    "legacy result_* acquisition artifact": re.compile(
        r"(?:[furbFURB]{0,2})?[\"']result_(?:\{[^}]+\}|[0-9])"
    ),
    "legacy url_* acquisition artifact": re.compile(
        r"(?:[furbFURB]{0,2})?[\"']url_(?:\{[^}]+\}|[0-9])"
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

# RC-8 final gate: supported runtime paths contain no legacy storage surface.
_LEGACY_SURFACE_ALLOWLIST: dict[tuple[str, str], int] = {}


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
        "legacy runtime storage surface changed; update the path-specific "
        "allowlist only for an intentional removal or approved migration:\n"
        f"actual={actual!r}"
    )


def test_rc8_runtime_inventory_is_empty():
    assert _legacy_surface_inventory(SCRIPTS.parent) == {}


def test_fscrape_has_no_legacy_storage_markers():
    inventory = _legacy_surface_inventory(SCRIPTS.parent)
    assert not any(path == "scripts/fscrape" for path, _marker in inventory)


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


def test_inventory_detects_indirect_file_handoffs_and_local_replay(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runtime.py").write_text(
        "search_path = '_search.json'\n"
        "corpus_path = '_corpus.json'\n"
        "context_path = '_context.json'\n"
        "reuse_search = True\n"
        "scrape_ranks = [1, 3]\n",
        encoding="utf-8",
    )

    inventory = _legacy_surface_inventory(tmp_path)
    assert inventory[("scripts/runtime.py", "_search.json")] == 1
    assert inventory[("scripts/runtime.py", "_corpus.json")] == 1
    assert inventory[("scripts/runtime.py", "_context.json")] == 1
    assert inventory[("scripts/runtime.py", "reuse_search")] == 1
    assert inventory[("scripts/runtime.py", "scrape_ranks")] == 1


def test_inventory_detects_remaining_legacy_artifact_family(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runtime.py").write_text(
        "candidate_path = '_candidates.json'\n"
        "index_path = '_index.md'\n"
        "workflow_input = '_workflow_input.json'\n"
        "raw_dir = '_raw'\n"
        "result_path = f'result_{rank}.md'\n"
        "url_path = f'url_{rank}.txt'\n",
        encoding="utf-8",
    )

    inventory = _legacy_surface_inventory(tmp_path)
    assert inventory[("scripts/runtime.py", "_candidates.json")] == 1
    assert inventory[("scripts/runtime.py", "_index.md")] == 1
    assert inventory[("scripts/runtime.py", "_workflow_input.json")] == 1
    assert inventory[("scripts/runtime.py", "legacy _raw acquisition directory")] == 1
    assert (
        inventory[("scripts/runtime.py", "legacy result_* acquisition artifact")] == 1
    )
    assert inventory[("scripts/runtime.py", "legacy url_* acquisition artifact")] == 1


def test_inventory_detects_removed_persistence_switches(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runtime.py").write_text(
        "FIRECRAWL_RESEARCH_ACTIVE = '1'\n"
        "FIRECRAWL_CAPTURE_RAW = '1'\n"
        "FIRECRAWL_RESEARCH_PERSIST = 'off'\n",
        encoding="utf-8",
    )

    inventory = _legacy_surface_inventory(tmp_path)
    assert inventory[("scripts/runtime.py", "FIRECRAWL_RESEARCH_ACTIVE")] == 1
    assert inventory[("scripts/runtime.py", "FIRECRAWL_CAPTURE_RAW")] == 1
    assert inventory[("scripts/runtime.py", "FIRECRAWL_RESEARCH_PERSIST")] == 1


def test_secure_ephemeral_and_explicit_export_files_are_not_legacy_storage(
    tmp_path: Path,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "atomic_export.py").write_text(
        "import tempfile\n"
        "from pathlib import Path\n"
        "with tempfile.NamedTemporaryFile(delete=True) as handle:\n"
        "    handle.write(b'ephemeral')\n"
        "export_path = Path('requested-report.json')\n"
        "export_path.write_text('{}')\n",
        encoding="utf-8",
    )

    assert _legacy_surface_inventory(tmp_path) == {}


class _SuccessfulSearchAdapter:
    def __init__(self):
        self.call_count = 0
        self.raw_payload = json.dumps(
            {
                "success": True,
                "data": [
                    {
                        "url": "https://example.com/authority",
                        "title": "Authority test",
                        "description": "Committed provider response",
                    }
                ],
            }
        ).encode()

    def search(self, _query_text: str, **_kwargs) -> SearchAdapterResult:
        self.call_count += 1
        return SearchAdapterResult(
            raw_payload=self.raw_payload,
            http_status=200,
            provider_request_id=f"authority-{self.call_count}",
            transport_error=None,
            transport_metadata={"attempt": self.call_count},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


class _CommitFailingUnitOfWork:
    def __init__(self, inner):
        self.inner = inner
        self.runs = None

    def __enter__(self):
        entered = self.inner.__enter__()
        self.connection = entered.connection
        self.runs = entered.runs
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)

    def commit(self):
        raise RuntimeError("simulated authoritative commit failure")


@pytest.fixture(scope="module")
def prepared_database():
    if not TEST_DSN:
        return None
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)
    return TEST_DSN


def _create_run(config: StoreConfig, objective: str):
    run_service = build_run_service(config)
    external_id = f"fr_{uuid4().hex}"
    run_service.create(objective=objective, external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return run_service, run_service.status(external_id=external_id)


@pytest.mark.skipif(
    not TEST_DSN,
    reason="requires explicit disposable PostgreSQL test DSN",
)
def test_guarded_success_is_visible_after_commit_and_retry_is_idempotent(
    tmp_path: Path,
    prepared_database,
):
    config = _config(tmp_path, TEST_DSN)
    run_service, status = _create_run(config, "issue 184 committed acquisition")
    adapter = _SuccessfulSearchAdapter()
    acquisition_service = build_acquisition_service(config, search_adapter=adapter)
    idempotency_key = f"authority:{uuid4()}"

    context = require_authoritative_acquisition(run_id=status.id, config=config)
    assert context.lifecycle_revision == status.lifecycle_revision
    result = acquisition_service.execute_search(
        status.id,
        "authoritative acquisition",
        idempotency_key=idempotency_key,
    )
    assert result.postgres_committed is True

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, run_id, raw_blob_sha256
            FROM search_responses
            WHERE run_id=%s AND idempotency_key=%s""",
            (status.id, idempotency_key),
        )
        committed = cursor.fetchall()
    assert len(committed) == 1
    assert committed[0][0] == result.search_response_id
    assert committed[0][1] == status.id
    assert re.fullmatch(r"[0-9a-f]{64}", committed[0][2])

    require_authoritative_acquisition(run_id=status.id, config=config)
    retried = acquisition_service.execute_search(
        status.id,
        "authoritative acquisition",
        idempotency_key=idempotency_key,
    )
    assert retried.search_response_id == result.search_response_id
    assert len(run_service.list_search_responses(status.id)) == 1
    assert retried.replayed is True
    assert adapter.call_count == 1


@pytest.mark.skipif(
    not TEST_DSN,
    reason="requires explicit disposable PostgreSQL test DSN",
)
def test_commit_failure_never_returns_success_and_retry_recovers(
    tmp_path: Path,
    prepared_database,
):
    config = _config(tmp_path, TEST_DSN)
    run_service, status = _create_run(config, "issue 184 commit failure")
    adapter = _SuccessfulSearchAdapter()
    normal_service = build_acquisition_service(config, search_adapter=adapter)
    base_factory = normal_service.uow_factory
    failing_service = AcquisitionService(
        lambda: _CommitFailingUnitOfWork(base_factory()),
        blob_store=normal_service.blob_store,
        search_adapter=cast(SearchAdapter, adapter),
        config=config,
        authority_preflight=require_authoritative_acquisition,
    )
    idempotency_key = f"authority-failure:{uuid4()}"

    require_authoritative_acquisition(run_id=status.id, config=config)
    with pytest.raises(RuntimeError, match="commit failure"):
        failing_service.execute_search(
            status.id,
            "commit must fail closed",
            idempotency_key=idempotency_key,
            replay_existing=False,
        )

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM search_responses WHERE idempotency_key=%s",
            (idempotency_key,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 0
    assert adapter.call_count == 0
    assert not any(path.is_file() for path in config.blob_root.rglob("*"))

    require_authoritative_acquisition(run_id=status.id, config=config)
    recovered = normal_service.execute_search(
        status.id,
        "commit must fail closed",
        idempotency_key=idempotency_key,
    )
    assert recovered.postgres_committed is True
    assert len(run_service.list_search_responses(status.id)) == 1
    assert adapter.call_count == 1
    assert any(path.is_file() for path in config.blob_root.rglob("*"))


def _database_url_for_role(database_url: str, role: str, password: str) -> str:
    parsed = urlsplit(database_url)
    hostname = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(role)}:{quote(password)}@{hostname}{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


@pytest.mark.skipif(
    not TEST_DSN,
    reason="requires explicit disposable PostgreSQL test DSN",
)
def test_restricted_role_fails_before_provider_execution(
    tmp_path: Path,
    prepared_database,
):
    admin_config = _config(tmp_path, TEST_DSN)
    _, status = _create_run(admin_config, "issue 184 restricted role")
    role = f"authority_probe_{uuid4().hex[:12]}"
    password = uuid4().hex

    role_created = False
    try:
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()
            assert database_name is not None
            database_name = database_name[0]
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.Literal(password),
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(role),
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON TABLE alembic_version TO {}").format(
                    sql.Identifier(role)
                )
            )
            _PRIVILEGE_SQL: dict[str, sql.SQL] = {
                "SELECT": sql.SQL("SELECT"),
                "INSERT": sql.SQL("INSERT"),
                "UPDATE": sql.SQL("UPDATE"),
            }
            for table, privileges in ACQUISITION_TABLE_PRIVILEGES.items():
                granted = set(privileges)
                if table == "research_events":
                    granted.discard("INSERT")
                privilege_sql = sql.SQL(", ").join(
                    _PRIVILEGE_SQL[p] for p in sorted(granted)
                )
                cursor.execute(
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        privilege_sql,
                        sql.Identifier(table),
                        sql.Identifier(role),
                    )
                )
        role_created = True

        restricted_url = _database_url_for_role(TEST_DSN, role, password)
        runner = mock.Mock()
        adapter = FirecrawlSearchAdapter(runner=runner)
        with pytest.raises(AcquisitionPreflightError, match="research_events:INSERT"):
            _guarded_search(
                adapter,
                run_id=status.id,
                config=_config(tmp_path, restricted_url),
                expected_heads_factory=None,
            )
        runner.assert_not_called()
    finally:
        if role_created:
            with connect(TEST_DSN) as connection, connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                cursor.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def test_acquisition_service_requires_preflight_before_provider_execution():
    adapter = mock.Mock()
    uow_factory = mock.Mock()
    service = AcquisitionService(uow_factory, search_adapter=adapter)

    with pytest.raises(AcquisitionPreflightError, match="preflight is required"):
        service.execute_search(uuid4(), "must not reach provider")

    adapter.search.assert_not_called()
    uow_factory.assert_not_called()


def test_acquisition_service_runs_injected_preflight_before_provider_execution():
    adapter = mock.Mock()
    uow_factory = mock.Mock()
    preflight = mock.Mock(side_effect=AcquisitionPreflightError("schema not ready"))
    config = object()
    service = AcquisitionService(
        uow_factory,
        search_adapter=adapter,
        config=config,
        authority_preflight=preflight,
    )
    run_id = uuid4()

    with pytest.raises(AcquisitionPreflightError, match="schema not ready"):
        service.execute_search(run_id, "must not reach provider")

    preflight.assert_called_once_with(run_id=run_id, config=config)
    adapter.search.assert_not_called()
    uow_factory.assert_not_called()


def test_evidence_packet_drops_legacy_scratch_file_result_field():
    from research_workflow import evidence_packet

    packet = evidence_packet(
        "objective",
        {"questions": ["question"]},
        [],
        [
            {
                "selected": True,
                "triage_candidate_id": "candidate-1",
                "url": "https://example.test",
                "scratch_file": "/tmp/firecrawl_scratch/result_001.md",
            }
        ],
        [],
        {},
        {},
        {},
    )

    serialized = json.dumps(packet, sort_keys=True)
    assert "scratch_file" not in serialized
    assert "firecrawl_scratch" not in serialized


def test_acquisition_search_cli_preflight_failure_precedes_service_construction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from types import SimpleNamespace

    import firecrawl_skill.research_store.acquisition.authority as acquisition_authority
    import firecrawl_skill.research_store.composition as container
    from firecrawl_skill.research_store import cli

    fake_config = mock.Mock()
    run_id = uuid4()
    monkeypatch.setattr(
        cli.StoreConfig,
        "from_env",
        classmethod(lambda _cls: fake_config),
    )
    monkeypatch.setattr(
        cli,
        "build_run_service",
        lambda _config: SimpleNamespace(
            status=lambda **_kwargs: SimpleNamespace(id=run_id)
        ),
    )
    preflight = mock.Mock(side_effect=AcquisitionPreflightError("schema not ready"))
    service_builder = mock.Mock()
    monkeypatch.setattr(
        acquisition_authority,
        "require_authoritative_acquisition",
        preflight,
    )
    monkeypatch.setattr(container, "build_acquisition_service", service_builder)

    code = cli.main(["acquisition-search", "fr_test", "query"])

    assert code == 2
    fake_config.require_database.assert_called_once_with()
    preflight.assert_called_once_with(run_id=run_id, config=fake_config)
    service_builder.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["failure_stage"] == "preflight"
    assert payload["status"] == "failed"


def test_acquisition_search_cli_returns_nonzero_for_persisted_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from types import SimpleNamespace

    import firecrawl_skill.research_store.acquisition.authority as acquisition_authority
    import firecrawl_skill.research_store.composition as container
    from firecrawl_skill.research_store import cli
    from firecrawl_skill.research_store.acquisition.authority import (
        AuthoritativeAcquisitionContext,
    )

    fake_config = mock.Mock()
    fake_config.database_url = "postgresql://research@test/research"
    run_id = uuid4()
    context = AuthoritativeAcquisitionContext(
        database_url=fake_config.database_url,
        run_id=run_id,
        run_state="acquiring",
        lifecycle_revision=1,
        schema_heads=frozenset({_SCHEMA_HEAD}),
        blob_root=Path("/tmp/blobs"),
        dry_run=False,
    )
    result = SimpleNamespace(
        search_response_id=uuid4(),
        run_id=run_id,
        query_text="query",
        backend="firecrawl",
        status="provider_error",
        candidate_count=0,
        postgres_committed=True,
        event_id=uuid4(),
        replayed=False,
    )
    service = SimpleNamespace(execute_search=mock.Mock(return_value=result))
    monkeypatch.setattr(
        cli.StoreConfig,
        "from_env",
        classmethod(lambda _cls: fake_config),
    )
    monkeypatch.setattr(
        cli,
        "build_run_service",
        lambda _config: SimpleNamespace(
            status=lambda **_kwargs: SimpleNamespace(id=run_id)
        ),
    )
    monkeypatch.setattr(
        acquisition_authority,
        "require_authoritative_acquisition",
        mock.Mock(return_value=context),
    )
    monkeypatch.setattr(container, "build_acquisition_service", lambda _config: service)

    code = cli.main(
        [
            "acquisition-search",
            "fr_test",
            "query",
            "--idempotency-key",
            "stable-key",
        ]
    )

    assert code == 1
    kwargs = service.execute_search.call_args.kwargs
    assert kwargs["authority_context"] is context
    assert kwargs["replay_existing"] is True
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "provider_error"
    assert payload["postgres_committed"] is True


def test_acquisition_search_cli_reports_idempotency_conflict_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from types import SimpleNamespace

    import firecrawl_skill.research_store.acquisition.authority as acquisition_authority
    import firecrawl_skill.research_store.composition as container
    from firecrawl_skill.research_store import cli
    from firecrawl_skill.research_store.acquisition.authority import (
        AuthoritativeAcquisitionContext,
    )
    from firecrawl_skill.research_store.acquisition.service import (
        AcquisitionIdempotencyConflictError,
    )

    fake_config = mock.Mock()
    fake_config.database_url = "postgresql://research@test/research"
    run_id = uuid4()
    context = AuthoritativeAcquisitionContext(
        database_url=fake_config.database_url,
        run_id=run_id,
        run_state="acquiring",
        lifecycle_revision=1,
        schema_heads=frozenset({_SCHEMA_HEAD}),
        blob_root=Path("/tmp/blobs"),
        dry_run=False,
    )
    service = SimpleNamespace(
        execute_search=mock.Mock(
            side_effect=AcquisitionIdempotencyConflictError(
                "search idempotency key was used for another request"
            )
        )
    )
    monkeypatch.setattr(
        cli.StoreConfig,
        "from_env",
        classmethod(lambda _cls: fake_config),
    )
    monkeypatch.setattr(
        cli,
        "build_run_service",
        lambda _config: SimpleNamespace(
            status=lambda **_kwargs: SimpleNamespace(id=run_id)
        ),
    )
    monkeypatch.setattr(
        acquisition_authority,
        "require_authoritative_acquisition",
        mock.Mock(return_value=context),
    )
    monkeypatch.setattr(container, "build_acquisition_service", lambda _config: service)

    code = cli.main(
        [
            "acquisition-search",
            "fr_test",
            "query",
            "--idempotency-key",
            "conflicting-key",
        ]
    )

    assert code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["failure_stage"] == "idempotency"
    assert "another request" in payload["error"]
