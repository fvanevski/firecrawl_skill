from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.acquisition_service import AcquisitionResult
from research_store.config import StoreConfig
from research_store.direct_scrape_service import DirectScrapePersistenceError
from research_store.domain import SearchAdapterResult, utcnow
from research_store.fsearch_service import (
    FSearchError,
    FSearchExtractionOutcome,
    FSearchRequest,
    FSearchResult,
    FSearchService,
    _emit_result,
    _exception_stage,
    main,
)
from research_store.postgres import connect, migrate, require_disposable_database_reset

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def prepared_database():
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 11


def _config(tmp_path: Path) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN or "",
        blob_root=tmp_path / "blobs",
    )


class CountingSearchAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, query_text: str, **_kwargs) -> SearchAdapterResult:
        self.calls += 1
        return SearchAdapterResult(
            raw_payload=json.dumps(
                {
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "url": "https://example.org/one",
                                "title": f"One for {query_text}",
                            },
                            {
                                "url": "https://example.org/two",
                                "title": f"Two for {query_text}",
                            },
                        ]
                    },
                }
            ).encode(),
            http_status=200,
            provider_request_id=f"request-{self.calls}",
            transport_error=None,
            transport_metadata={"test": True, "implicit_scrape": False},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


def _new_run(config: StoreConfig, objective: str = "review regression"):
    from research_store.container import (
        build_run_service,
        build_workflow_operation_service,
    )

    external_id = f"fr_{uuid4().hex}"
    service = build_run_service(config)
    run = service.create(objective=objective, external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return service, run, external_id


def _build_service(config: StoreConfig, adapter):
    from research_store.fsearch_service import build_fsearch_service

    return build_fsearch_service(config, search_adapter_factory=lambda: adapter)


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL")
def test_commit_revalidates_preflight_revision_before_persisting(tmp_path):
    config = _config(tmp_path)
    run_service, run, external_id = _new_run(config, "stale authority")

    class StaleRunAdapter(CountingSearchAdapter):
        def search(self, query_text: str, **kwargs) -> SearchAdapterResult:
            with connect(TEST_DSN) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE research_runs
                       SET state='extracting', lifecycle_revision=lifecycle_revision+1
                       WHERE id=%s""",
                    (run.id,),
                )
            return super().search(query_text, **kwargs)

    adapter = StaleRunAdapter()
    service = _build_service(config, adapter)

    with pytest.raises(FSearchError) as caught:
        service.execute(
            FSearchRequest(
                "stale authority query",
                external_id,
                scrape_limit=0,
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )

    assert caught.value.stage == "ingestion"
    assert adapter.calls == 1
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM search_responses WHERE run_id=%s", (run.id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM search_candidates WHERE run_id=%s", (run.id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM research_events WHERE run_id=%s AND event_type='acquisition.search_executed'",
            (run.id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM research_invocations WHERE run_id=%s AND operation='direct_scrape'",
            (run.id,),
        )
        assert cursor.fetchone()[0] == 0
    assert run_service.status(run_id=run.id).state == "extracting"


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL")
def test_explicit_key_replays_before_provider_and_validates_request(tmp_path):
    config = _config(tmp_path)
    _run_service, _run, external_id = _new_run(config, "explicit replay")
    adapter = CountingSearchAdapter()
    service = _build_service(config, adapter)
    key = f"review-replay-{uuid4()}"

    first = service.execute(
        FSearchRequest(
            "same request",
            external_id,
            scrape_limit=0,
            idempotency_key=key,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    second = service.execute(
        FSearchRequest(
            "same request",
            external_id,
            scrape_limit=0,
            idempotency_key=key,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )

    assert adapter.calls == 1
    assert second.search_replayed is True
    assert second.search_response_id == first.search_response_id
    assert second.candidate_ids == first.candidate_ids

    with pytest.raises(FSearchError) as caught:
        service.execute(
            FSearchRequest(
                "different request",
                external_id,
                scrape_limit=0,
                idempotency_key=key,
                external_invocation_id=f"fc_{uuid4().hex}",
            )
        )
    assert caught.value.stage == "ingestion"
    assert "another request" in str(caught.value)
    assert adapter.calls == 1


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL")
def test_default_key_is_fresh_per_invocation(tmp_path):
    config = _config(tmp_path)
    run_service, run, external_id = _new_run(config, "fresh defaults")
    adapter = CountingSearchAdapter()
    service = _build_service(config, adapter)

    first = service.execute(
        FSearchRequest(
            "fresh request",
            external_id,
            scrape_limit=0,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    second = service.execute(
        FSearchRequest(
            "fresh request",
            external_id,
            scrape_limit=0,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )

    assert adapter.calls == 2
    assert first.search_response_id != second.search_response_id
    assert second.search_replayed is False
    assert len(run_service.list_search_responses(run.id)) == 2


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL")
def test_retry_after_outer_completion_crash_replays_committed_search(tmp_path):
    from research_store.container import build_invocation_service

    config = _config(tmp_path)
    run_service, run, external_id = _new_run(config, "crash recovery")
    adapter = CountingSearchAdapter()
    real = _build_service(config, adapter)
    delegate = build_invocation_service(config)

    class CrashOnceInvocationService:
        def __init__(self):
            self.crashed = False

        def begin(self, *args, **kwargs):
            return delegate.begin(*args, **kwargs)

        def complete(self, *args, **kwargs):
            if not self.crashed and args[2] == "succeeded":
                self.crashed = True
                raise RuntimeError("simulated crash after acquisition commit")
            return delegate.complete(*args, **kwargs)

    crash = CrashOnceInvocationService()
    service = FSearchService(
        config,
        run_service,
        crash,
        acquisition_factory=real.acquisition_factory,
        direct_scrape_factory=real.direct_scrape_factory,
        preflight=real.preflight,
        classify_target=real.classify_target,
        profiles=real.profiles,
    )
    invocation_id = f"fc_{uuid4().hex}"
    request = FSearchRequest(
        "crash recovery request",
        external_id,
        scrape_limit=0,
        external_invocation_id=invocation_id,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.execute(request)
    assert adapter.calls == 1
    assert len(run_service.list_search_responses(run.id)) == 1

    recovered = service.execute(request)
    assert recovered.search_replayed is True
    assert adapter.calls == 1
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM research_invocations WHERE external_invocation_id=%s",
            (invocation_id,),
        )
        assert cursor.fetchone()[0] == "complete"


def test_fsearch_uses_stable_candidate_id_not_occurrence_id():
    run_id = uuid4()
    occurrence_id = uuid4()
    candidate_id = uuid4()
    direct_calls = []

    acquisition = AcquisitionResult(
        search_response_id=uuid4(),
        run_id=run_id,
        query_text="candidate identity",
        backend="firecrawl",
        status="succeeded",
        candidate_count=1,
        candidates=[
            {
                "id": occurrence_id,
                "candidate_id": candidate_id,
                "rank": 1,
                "original_url": "https://example.org/stable",
            }
        ],
        postgres_committed=True,
    )

    class Direct:
        def execute(self, _run_id, requests, **_kwargs):
            direct_calls.extend(requests)
            return SimpleNamespace(
                invocation_id=uuid4(),
                status="complete",
                replayed=False,
                items=(
                    SimpleNamespace(
                        candidate_id=candidate_id,
                        status="succeeded",
                        error=None,
                        failure_class=None,
                        extraction_attempt_id=uuid4(),
                        source_id=uuid4(),
                        snapshot_id=uuid4(),
                        document_id=uuid4(),
                        derivation_id=uuid4(),
                        chunk_ids=(uuid4(),),
                    ),
                ),
            )

    invocations = SimpleNamespace(
        begin=lambda *_args, **_kwargs: SimpleNamespace(id=uuid4()),
        complete=lambda *_args, **_kwargs: None,
    )
    service = FSearchService(
        SimpleNamespace(),
        SimpleNamespace(status=lambda **_kwargs: SimpleNamespace(id=run_id)),
        invocations,
        acquisition_factory=lambda: SimpleNamespace(
            execute_search=lambda *_args, **_kwargs: acquisition
        ),
        direct_scrape_factory=Direct,
        preflight=lambda **_kwargs: SimpleNamespace(
            run_id=run_id, lifecycle_revision=0
        ),
        classify_target=lambda *_args: ("other", False),
        profiles={},
    )

    result = service.execute(
        FSearchRequest(
            "candidate identity",
            f"fr_{uuid4().hex}",
            scrape_limit=1,
        )
    )
    assert result.candidate_ids == (candidate_id,)
    assert direct_calls[0].candidate_id == candidate_id
    assert direct_calls[0].candidate_id != occurrence_id


def test_output_contract_is_bounded_and_human_readable(capsys):
    outcomes = tuple(
        FSearchExtractionOutcome(
            candidate_id=uuid4(),
            status="succeeded",
            extraction_attempt_id=uuid4(),
            source_id=uuid4(),
            snapshot_id=uuid4(),
            document_id=uuid4(),
            derivation_id=uuid4(),
            chunk_ids=tuple(uuid4() for _ in range(40)),
        )
        for _ in range(25)
    )
    result = FSearchResult(
        status="complete",
        run_id=uuid4(),
        research_run_id=f"fr_{uuid4().hex}",
        invocation_id=uuid4(),
        external_invocation_id=f"fc_{uuid4().hex}",
        search_response_id=uuid4(),
        candidate_ids=tuple(uuid4() for _ in range(120)),
        search_replayed=True,
        extraction_invocation_id=uuid4(),
        extraction_status="complete",
        extraction_outcomes=outcomes,
    )
    payload = result.to_dict()
    assert payload["candidate_count"] == 120
    assert len(payload["candidate_ids"]) == 100
    assert payload["candidate_ids_truncated"] is True
    assert payload["extraction_outcome_count"] == 25
    assert len(payload["extraction_outcomes"]) == 20
    assert payload["extraction_outcomes_truncated"] is True
    assert len(payload["extraction_outcomes"][0]["chunk_ids"]) == 25
    assert payload["extraction_outcomes"][0]["chunk_ids_truncated"] is True
    assert payload["corpus_ids"]["chunk_count"] == 1000
    assert len(payload["corpus_ids"]["chunk_ids"]) == 100
    assert payload["corpus_ids"]["chunk_ids_truncated"] is True

    _emit_result(result, False)
    output = capsys.readouterr().out
    for label in (
        "research_run_id:",
        "external_invocation_id:",
        "candidate_ids:",
        "extraction_invocation_id:",
        "extraction_outcomes:",
        "corpus_ids:",
    ):
        assert label in output


def test_json_argument_errors_are_structured(capsys):
    constructed = False

    def factory():
        nonlocal constructed
        constructed = True
        raise AssertionError("service must not be built")

    code = main(["query", "--json"], service_factory=factory)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_stage"] == "preflight"
    assert "research-run-id" in payload["error"]
    assert constructed is False

    code = main(
        [
            "query",
            "--research-run-id",
            f"fr_{uuid4().hex}",
            "--dir",
            "/tmp/legacy",
            "--json",
        ],
        service_factory=factory,
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert "--dir was removed" in payload["error"]
    assert constructed is False


def test_typed_persistence_stage_does_not_depend_on_message():
    error = DirectScrapePersistenceError(
        "opaque database failure without index keywords", stage="indexing"
    )
    assert _exception_stage(error) == "indexing"


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL")
def test_public_launcher_success_is_authoritative_and_scratch_free(tmp_path):
    config = _config(tmp_path)
    _run_service, run, external_id = _new_run(config, "launcher integration")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "firecrawl-calls.jsonl"
    firecrawl = bin_dir / "firecrawl"
    firecrawl.write_text(
        textwrap.dedent(
            r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_FIRECRAWL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if "-o" in args:
    print("filesystem output is forbidden", file=sys.stderr)
    raise SystemExit(91)
if not args or args[0] in {"--version", "version"}:
    print("9.9.9-review")
    raise SystemExit(0)
if args[0] == "search":
    print(json.dumps({
        "success": True,
        "data": {"web": [
            {"url": "https://example.org/a", "title": "A"},
            {"url": "https://example.org/b", "title": "B"}
        ]}
    }))
    raise SystemExit(0)
if args[0] == "scrape":
    print("# Authoritative page\n\n" + "substantive evidence content " * 160)
    raise SystemExit(0)
print("unsupported command", file=sys.stderr)
raise SystemExit(2)
"""
        ),
        encoding="utf-8",
    )
    firecrawl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_FIRECRAWL_LOG": str(call_log),
            "DATABASE_URL": TEST_DSN,
            "BLOB_ROOT": str(config.blob_root),
            "TMPDIR": str(tmp_path / "tmp"),
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            "FIRECRAWL_RESEARCH_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.pop("QDRANT_URL", None)
    env.pop("QDRANT_API_KEY", None)
    env.pop("VALKEY_URL", None)
    Path(env["TMPDIR"]).mkdir()

    result = subprocess.run(
        [
            str(SCRIPTS / "fsearch"),
            "launcher integration query",
            "--research-run-id",
            external_id,
            "--limit",
            "2",
            "--scrape-limit",
            "1",
            "--json",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "complete"
    assert payload["search_response_id"]
    assert payload["candidate_count"] == 2
    assert payload["extraction_outcome_count"] == 1
    assert payload["extraction_outcomes"][0]["status"] == "succeeded"
    assert payload["corpus_ids"]["source_ids"]
    assert payload["corpus_ids"]["snapshot_ids"]
    assert payload["corpus_ids"]["document_ids"]
    assert payload["corpus_ids"]["chunk_ids"]

    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    assert any(call and call[0] == "search" for call in calls)
    assert any(call and call[0] == "scrape" for call in calls)
    assert all("-o" not in call for call in calls)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        expected = {
            "search_responses": 1,
            "search_candidates": 2,
            "candidate_occurrences": 2,
            "extraction_attempts": 1,
            "sources": 1,
            "asset_snapshots": 1,
            "documents": 1,
        }
        for table, minimum in expected.items():
            cursor.execute(f"SELECT count(*) FROM {table}")
            assert cursor.fetchone()[0] >= minimum, table
        cursor.execute("SELECT count(*) FROM index_jobs")
        assert cursor.fetchone()[0] > 0
        cursor.execute(
            "SELECT candidate_id FROM extraction_attempts WHERE run_id=%s",
            (run.id,),
        )
        extracted_candidate = cursor.fetchone()[0]
        assert (
            str(extracted_candidate)
            == payload["extraction_outcomes"][0]["candidate_id"]
        )

    assert any(path.is_file() for path in config.blob_root.rglob("*"))
    forbidden = (
        "_search.json",
        "_meta.json",
        "_context.json",
        "_candidates.json",
        "_index.md",
        "_corpus.json",
    )
    assert all(not list(tmp_path.rglob(name)) for name in forbidden)
    assert not list(tmp_path.rglob("result_*"))
    assert not list(tmp_path.rglob("firecrawl_scratch"))


def test_launcher_sources_research_env_and_uses_configured_python(tmp_path):
    wrapper = tmp_path / "fsearch"
    shutil.copy2(SCRIPTS / "fsearch", wrapper)
    (tmp_path / "research_store").symlink_to(
        SCRIPTS / "research_store", target_is_directory=True
    )
    log = tmp_path / "interpreter.log"
    interpreter = tmp_path / "configured-python"
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "${BOOTSTRAP_SENTINEL:-}" > "$BOOTSTRAP_LOG"\n'
        'exec "$REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    (tmp_path / "research-env").write_text(
        f'export FIRECRAWL_RESEARCH_PYTHON="{interpreter}"\n'
        'export BOOTSTRAP_SENTINEL="research-env-loaded"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "BOOTSTRAP_LOG": str(log),
            "REAL_PYTHON": sys.executable,
            "FIRECRAWL_RESEARCH_AUTO_ENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.pop("FIRECRAWL_RESEARCH_PYTHON", None)
    env["PYTHONPATH"] = str(SCRIPTS)

    result = subprocess.run(
        [str(wrapper), "--help"],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--research-run-id" in result.stdout
    assert log.read_text().strip() == "research-env-loaded"
