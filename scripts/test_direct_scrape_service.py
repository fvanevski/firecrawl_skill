from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.acquisition_authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
)
from research_store.blob import ContentAddressedBlobStore
from research_store.config import StoreConfig
from research_store.direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapePersistenceError,
    DirectScrapeRequest,
    DirectScrapeService,
    FirecrawlDirectScrapeAdapter,
    ScrapeTransportResult,
    _ResolvedTarget,
)
from research_store.domain import IngestRequest
from research_store.inspection_service import InspectionService
from research_store.parsing import get_registry
from research_store.postgres import PostgresUnitOfWork, connect
from research_store.service import CorpusService

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


class _Runner:
    def __init__(self, *, stdout=b"# Title\n\nBody", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_adapter_captures_bytes_without_output_file():
    runner = _Runner(stdout=b'{"answer": 42}')
    version_runner = _Runner(stdout=b"firecrawl 1.9.0\n")
    adapter = FirecrawlDirectScrapeAdapter(
        runner=runner,
        version_runner=version_runner,
    )

    result = adapter.scrape(
        "https://example.com/report",
        format="markdown",
        schema={"type": "object"},
    )

    command, kwargs = runner.commands[0]
    assert "-o" not in command
    assert "--schema" in command
    assert command[command.index("--format") + 1] == "json"
    assert kwargs["capture_output"] is True
    assert result.raw_payload == b'{"answer": 42}'
    assert result.succeeded is True


def test_request_requires_exactly_one_target():
    assert "source_path" not in DirectScrapeRequest.__dataclass_fields__
    assert "manifest_path" not in DirectScrapeRequest.__dataclass_fields__
    with pytest.raises(ValueError, match="exactly one"):
        DirectScrapeRequest()
    with pytest.raises(ValueError, match="exactly one"):
        DirectScrapeRequest(
            url="https://example.com",
            candidate_id=uuid4(),
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        DirectScrapeRequest(
            url="https://example.com",
            schema={"type": "object"},
            summary=True,
        )


def _context(run_id: UUID) -> AuthoritativeAcquisitionContext:
    return AuthoritativeAcquisitionContext(
        database_url="postgresql://test",
        blob_root=Path("/tmp/blobs"),
        schema_heads=frozenset({"head"}),
        run_id=run_id,
        run_state="acquiring",
        lifecycle_revision=3,
        dry_run=False,
    )


class _NeverAdapter:
    constructed = 0

    def __init__(self):
        type(self).constructed += 1

    def scrape(self, *_args, **_kwargs):
        raise AssertionError("transport must not run")


def test_failed_preflight_does_not_construct_transport():
    _NeverAdapter.constructed = 0

    def fail_preflight(**_kwargs):
        raise AcquisitionPreflightError("database unavailable")

    service = DirectScrapeService(
        StoreConfig.from_env(),
        lambda: None,
        object(),
        object(),
        adapter_factory=_NeverAdapter,
        preflight=fail_preflight,
    )

    with pytest.raises(AcquisitionPreflightError, match="database unavailable"):
        service.execute(uuid4(), [DirectScrapeRequest(url="https://example.com")])
    assert _NeverAdapter.constructed == 0


def test_failed_direct_persistence_check_does_not_construct_transport():
    _NeverAdapter.constructed = 0
    run_id = uuid4()

    def fail_authority(_uow_factory):
        raise RuntimeError("missing extraction privilege")

    service = DirectScrapeService(
        StoreConfig.from_env(),
        lambda: None,
        object(),
        object(),
        adapter_factory=_NeverAdapter,
        preflight=lambda **_kwargs: _context(run_id),
        authority_check=fail_authority,
    )

    with pytest.raises(RuntimeError, match="missing extraction privilege"):
        service.execute(run_id, [DirectScrapeRequest(url="https://example.com")])
    assert _NeverAdapter.constructed == 0


class _OrchestrationService(DirectScrapeService):
    def __init__(self, transports):
        self.run_id = uuid4()
        self.invocation_id = uuid4()
        self.transports = iter(transports)
        self.finalized = None
        self.adapter_constructed = 0
        super().__init__(
            StoreConfig.from_env(),
            lambda: None,
            object(),
            object(),
            adapter_factory=self._adapter_factory,
            preflight=lambda **_kwargs: _context(self.run_id),
            authority_check=lambda _uow_factory: None,
        )

    def _adapter_factory(self):
        self.adapter_constructed += 1
        parent = self

        class Adapter:
            def scrape(self, *_args, **_kwargs):
                return next(parent.transports)

        return Adapter()

    @contextmanager
    def _claim_item(self, _context_value, _invocation_id, _item_key):
        yield None

    def _resolve_existing_candidates(self, _run_id, requests):
        return {
            index: {
                "id": uuid4(),
                "canonical_url": request.url,
                "original_url": request.url,
                "title": None,
            }
            for index, request in enumerate(requests)
        }

    def _begin_or_resume(
        self,
        _context_value,
        _requests,
        _candidates,
        _idempotency_key,
        _external_invocation_id,
        _parent_invocation_id,
    ):
        return self.invocation_id, {}, None

    def _load_resolved_targets(
        self,
        _run_id,
        invocation_id,
        requests,
        candidates,
        batch_key,
        _retry_parent_attempt_ids,
    ):
        return tuple(
            _ResolvedTarget(
                index=index,
                item_key=self._item_key(batch_key, index, request, request.url),
                request=request,
                candidate_id=UUID(str(candidates[index]["id"])),
                requested_url=request.url,
                canonical_url=request.url,
                title=None,
            )
            for index, request in enumerate(requests)
        )

    def _persist_success(self, _context_value, invocation_id, target, transport):
        return DirectScrapeItemResult(
            index=target.index,
            item_key=target.item_key,
            status="succeeded",
            requested_url=target.requested_url,
            canonical_url=target.canonical_url,
            candidate_id=target.candidate_id,
            invocation_id=invocation_id,
            format=target.request.effective_format,
            mime_type=target.request.effective_mime_type,
            raw_blob_sha256="a" * 64,
        )

    def _persist_failure(
        self, _context_value, invocation_id, target, transport, **_kwargs
    ):
        return DirectScrapeItemResult(
            index=target.index,
            item_key=target.item_key,
            status="failed",
            requested_url=target.requested_url,
            canonical_url=target.canonical_url,
            candidate_id=target.candidate_id,
            invocation_id=invocation_id,
            format=target.request.effective_format,
            mime_type=target.request.effective_mime_type,
            error=transport.stderr.decode(),
        )

    def _finalize_invocation(
        self, _context_value, _invocation_id, _idempotency_key, status, items
    ):
        self.finalized = (status, tuple(items))


def test_multi_url_partial_failure_is_explicit_and_ordered():
    service = _OrchestrationService(
        [
            ScrapeTransportResult(raw_payload=b"first"),
            ScrapeTransportResult(
                raw_payload=b"",
                returncode=1,
                stderr=b"blocked",
            ),
        ]
    )

    result = service.execute(
        service.run_id,
        [
            DirectScrapeRequest(url="https://example.com/1"),
            DirectScrapeRequest(url="https://example.com/2"),
        ],
        idempotency_key="batch-1",
    )

    assert isinstance(result, DirectScrapeBatchResult)
    assert result.status == "partial"
    assert [item.index for item in result.items] == [0, 1]
    assert [item.status for item in result.items] == ["succeeded", "failed"]
    assert result.items[1].error == "blocked"
    assert service.finalized[0] == "partial"


def _integration() -> None:
    if not TEST_DSN:
        pytest.skip("RESEARCH_STORE_TEST_DATABASE_URL is not configured")


def _config(tmp_path: Path) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )


def _uow_factory(config: StoreConfig):
    return partial(
        PostgresUnitOfWork,
        config.database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
    )


def _insert_run(run_id: UUID) -> None:
    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs(
            id,objective,query_plan,skill_version,llm_model,state,execution_mode)
            VALUES(
              %s,'direct scrape test','{}','test','test','acquiring','agent_led'
            )""",
            (run_id,),
        )
        connection.commit()


class _SequenceAdapter:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def scrape(self, url, **_kwargs):
        self.calls.append(url)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _build_service(tmp_path: Path, adapter_factory):
    config = _config(tmp_path)
    uow_factory = _uow_factory(config)
    blob_store = ContentAddressedBlobStore(config.blob_root)
    corpus = CorpusService(
        config,
        uow_factory,
        blob_store,
        parser_registry=get_registry(),
    )
    return DirectScrapeService(
        config,
        uow_factory,
        blob_store,
        corpus,
        adapter_factory=adapter_factory,
    )


class _RevisionBumpAdapter:
    def __init__(self, run_id: UUID):
        self.run_id = run_id
        self.calls = []

    def scrape(self, url, **_kwargs):
        self.calls.append(url)
        with connect(TEST_DSN) as connection, connection.cursor() as cur:
            cur.execute(
                """UPDATE research_runs
                SET lifecycle_revision=lifecycle_revision+1 WHERE id=%s""",
                (self.run_id,),
            )
            connection.commit()
        return ScrapeTransportResult(raw_payload=b"# Stale\n\nMust not commit.")


def test_direct_scrape_rejects_stale_revision_after_transport(tmp_path: Path):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)
    adapter = _RevisionBumpAdapter(run_id)

    with pytest.raises(
        DirectScrapePersistenceError,
        match="lifecycle revision changed",
    ):
        _build_service(tmp_path, lambda: adapter).execute(
            run_id,
            [DirectScrapeRequest(url="https://example.com/stale")],
            idempotency_key="integration-stale-revision",
        )

    assert adapter.calls == ["https://example.com/stale"]
    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM extraction_attempts WHERE run_id=%s""",
            (run_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """SELECT count(*) FROM asset_snapshots s
            JOIN sources src ON src.id=s.source_id
            WHERE src.canonical_url=%s""",
            ("https://example.com/stale",),
        )
        assert cur.fetchone()[0] == 0


def test_direct_scrape_postgres_url_json_partial_retry_and_recovery(tmp_path: Path):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)

    first_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(raw_payload=b"# First\n\nAuthoritative body."),
            RuntimeError("simulated worker crash"),
        ]
    )
    service = _build_service(tmp_path, lambda: first_adapter)
    requests = [
        DirectScrapeRequest(url="https://example.com/first"),
        DirectScrapeRequest(
            url="https://example.com/structured",
            schema={"type": "object", "required": ["answer"]},
        ),
    ]

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        service.execute(run_id, requests, idempotency_key="integration-crash")

    second_adapter = _SequenceAdapter(
        [ScrapeTransportResult(raw_payload=b'{"answer": 42, "ok": true}')]
    )
    resumed = _build_service(tmp_path, lambda: second_adapter).execute(
        run_id,
        requests,
        idempotency_key="integration-crash",
    )

    assert resumed.status == "complete"
    assert resumed.replayed is False
    assert first_adapter.calls == [
        "https://example.com/first",
        "https://example.com/structured",
    ]
    assert second_adapter.calls == ["https://example.com/structured"]
    assert resumed.items[0].mime_type == "text/markdown"
    assert resumed.items[1].mime_type == "application/json"
    assert all(item.snapshot_id for item in resumed.items)
    assert all(item.document_id for item in resumed.items)
    assert all(item.derivation_id for item in resumed.items)
    assert all(item.chunk_ids for item in resumed.items)
    assert all(item.raw_blob_sha256 for item in resumed.items)

    replay_adapter = _SequenceAdapter([])
    replayed = _build_service(tmp_path, lambda: replay_adapter).execute(
        run_id,
        requests,
        idempotency_key="integration-crash",
    )
    assert replayed.replayed is True
    assert replayed.to_dict()["items"] == resumed.to_dict()["items"]
    assert replay_adapter.calls == []

    candidate_adapter = _SequenceAdapter(
        [ScrapeTransportResult(raw_payload=b"# Updated\n\nCandidate-ID body.")]
    )
    candidate_result = _build_service(tmp_path, lambda: candidate_adapter).execute(
        run_id,
        [DirectScrapeRequest(candidate_id=resumed.items[0].candidate_id)],
        idempotency_key="integration-candidate-id",
    )
    assert candidate_result.status == "complete"
    assert candidate_result.items[0].candidate_id == resumed.items[0].candidate_id
    assert candidate_adapter.calls == ["https://example.com/first"]

    partial_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(raw_payload=b"# Good\n\nGood body."),
            ScrapeTransportResult(
                raw_payload=b"",
                returncode=1,
                stderr=b"upstream denied request",
                metadata={"failure_class": "http_error"},
            ),
        ]
    )
    partial = _build_service(tmp_path, lambda: partial_adapter).execute(
        run_id,
        [
            DirectScrapeRequest(url="https://example.com/good"),
            DirectScrapeRequest(url="https://example.com/bad"),
        ],
        idempotency_key="integration-partial",
    )
    assert partial.status == "partial"
    assert [item.status for item in partial.items] == ["succeeded", "failed"]

    partial_replay_adapter = _SequenceAdapter([])
    partial_replay = _build_service(tmp_path, lambda: partial_replay_adapter).execute(
        run_id,
        [
            DirectScrapeRequest(url="https://example.com/good"),
            DirectScrapeRequest(url="https://example.com/bad"),
        ],
        idempotency_key="integration-partial",
    )
    assert partial_replay.replayed is True
    assert partial_replay.status == "partial"
    assert partial_replay_adapter.calls == []

    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM extraction_attempts
            WHERE run_id=%s AND invocation_id=%s""",
            (run_id, resumed.invocation_id),
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT output::text FROM research_invocations WHERE id=%s",
            (resumed.invocation_id,),
        )
        invocation_output = cur.fetchone()[0]
        assert "Authoritative body" not in invocation_output
        assert '"answer": 42' not in invocation_output
        cur.execute(
            """SELECT count(*) FROM research_run_assets WHERE run_id=%s""",
            (run_id,),
        )
        assert cur.fetchone()[0] == 4
        cur.execute(
            """SELECT count(*) FROM index_jobs j
            JOIN chunks c ON c.id=j.entity_id
            JOIN documents d ON d.id=c.document_id
            JOIN asset_snapshots s ON s.id=d.snapshot_id
            JOIN research_run_assets r ON r.snapshot_id=s.id
            WHERE r.run_id=%s""",
            (run_id,),
        )
        assert cur.fetchone()[0] >= 4


def test_summary_is_a_canonical_format_and_mime_contract():
    request = DirectScrapeRequest(
        url="https://example.com/summary",
        summary=True,
    )
    assert request.effective_format == "summary"
    assert request.effective_summary is True
    assert request.effective_mime_type == "text/plain"

    runner = _Runner(stdout=b"Concise summary")
    version_runner = _Runner(stdout=b"firecrawl 1.9.0\n")
    adapter = FirecrawlDirectScrapeAdapter(
        runner=runner,
        version_runner=version_runner,
    )
    result = adapter.scrape(
        "https://example.com/summary",
        format=request.effective_format,
        summary=request.effective_summary,
    )
    command, _kwargs = runner.commands[0]
    assert command[command.index("--format") + 1] == "summary"
    assert "--summary" not in command
    assert result.metadata["firecrawl_cli_version"] == "firecrawl 1.9.0"

    with pytest.raises(ValueError, match="incompatible"):
        DirectScrapeRequest(
            url="https://example.com/summary",
            summary=True,
            mime_type="text/html",
        )
    with pytest.raises(ValueError, match="another format"):
        DirectScrapeRequest(
            url="https://example.com/summary",
            format="html",
            summary=True,
        )


@pytest.mark.parametrize(
    ("format_name", "mime_type"),
    [
        ("markdown", "text/markdown"),
        ("html", "text/html"),
        ("rawHtml", "text/html"),
        ("json", "application/json"),
        ("links", "application/json"),
        ("images", "application/json"),
        ("summary", "text/plain"),
    ],
)
def test_supported_formats_have_explicit_mime_contract(format_name, mime_type):
    request = DirectScrapeRequest(
        url=f"https://example.com/{format_name}",
        format=format_name,
    )
    assert request.effective_format == format_name
    assert request.effective_mime_type == mime_type


class _BlockingAdapter:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def scrape(self, url, **_kwargs):
        with self.lock:
            self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return ScrapeTransportResult(
            raw_payload=b"# Serialized\n\nOne provider execution.",
            provider_request_id="provider-one",
            metadata={"firecrawl_cli_version": "test-cli"},
        )


def test_concurrent_same_key_executes_provider_once(tmp_path: Path):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)
    adapter = _BlockingAdapter()
    request = [DirectScrapeRequest(url="https://example.com/concurrent")]

    def invoke():
        return _build_service(tmp_path, lambda: adapter).execute(
            run_id,
            request,
            idempotency_key="integration-concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke)
        assert adapter.started.wait(timeout=10)
        second = executor.submit(invoke)
        time.sleep(0.3)
        assert adapter.calls == 1
        adapter.release.set()
        first_result = first.result(timeout=20)
        second_result = second.result(timeout=20)

    assert adapter.calls == 1
    assert first_result.items[0].to_dict() == second_result.items[0].to_dict()
    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM extraction_attempts
            WHERE run_id=%s AND candidate_id=%s""",
            (run_id, first_result.items[0].candidate_id),
        )
        assert cur.fetchone()[0] == 1


def test_format_parser_identity_retry_lineage_and_transport_provenance(
    tmp_path: Path,
):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)

    formats = [
        ("markdown", b"# Markdown\n\nBody.", "MarkdownParser", "text/markdown"),
        (
            "html",
            b"<html><body><main><h1>HTML</h1><p>Body.</p></main></body></html>",
            "HtmlMainContentParser",
            "text/html",
        ),
        ("json", b'{"answer": 42}', "JsonParser", "application/json"),
        ("links", b'["https://example.com/a"]', "JsonParser", "application/json"),
        ("images", b'["https://example.com/a.png"]', "JsonParser", "application/json"),
        ("summary", b"Concise summary body.", "PlainTextParser", "text/plain"),
    ]
    for format_name, payload, parser_name, mime_type in formats:
        adapter = _SequenceAdapter(
            [
                ScrapeTransportResult(
                    raw_payload=payload,
                    provider_request_id=f"provider-{format_name}",
                    metadata={
                        "firecrawl_cli_version": "test-cli",
                        "format": format_name,
                    },
                )
            ]
        )
        result = _build_service(tmp_path, lambda adapter=adapter: adapter).execute(
            run_id,
            [
                DirectScrapeRequest(
                    url=f"https://example.com/formats/{format_name}",
                    format=format_name,
                )
            ],
            idempotency_key=f"integration-format-{format_name}",
        )
        item = result.items[0]
        assert item.mime_type == mime_type
        with connect(TEST_DSN) as connection, connection.cursor() as cur:
            cur.execute(
                """SELECT d.parser_name,d.parser_version,a.raw_blob_mime_type
                FROM documents d
                JOIN extraction_attempts a ON a.id=d.extraction_attempt_id
                WHERE d.id=%s""",
                (item.document_id,),
            )
            row = cur.fetchone()
            assert parser_name in row[0]
            assert row[1]
            assert row[2] == mime_type
            cur.execute(
                """SELECT payload
                FROM research_events
                WHERE run_id=%s
                  AND event_type='direct_scrape_transport_recorded'
                  AND payload->>'extraction_attempt_id'=%s""",
                (run_id, str(item.extraction_attempt_id)),
            )
            event = cur.fetchone()[0]
            assert event["provider_request_id"] == f"provider-{format_name}"
            assert event["format"] == format_name
            assert event["mime_type"] == mime_type

    failure_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(
                raw_payload=b"",
                returncode=1,
                stderr=b"temporary upstream failure",
                provider_request_id="provider-failure",
                metadata={
                    "failure_class": "network",
                    "firecrawl_cli_version": "test-cli",
                },
            )
        ]
    )
    failed = _build_service(tmp_path, lambda: failure_adapter).execute(
        run_id,
        [DirectScrapeRequest(url="https://example.com/retry")],
        idempotency_key="integration-retry-original",
    )
    assert failed.status == "failed"

    replay_adapter = _SequenceAdapter([])
    replay = _build_service(tmp_path, lambda: replay_adapter).execute(
        run_id,
        [DirectScrapeRequest(url="https://example.com/retry")],
        idempotency_key="integration-retry-original",
    )
    assert replay.replayed is True
    assert replay_adapter.calls == []

    retry_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(
                raw_payload=b"# Retry succeeded\n\nAuthoritative.",
                provider_request_id="provider-retry",
                metadata={"firecrawl_cli_version": "test-cli"},
            )
        ]
    )
    retried = _build_service(tmp_path, lambda: retry_adapter).retry_failed(
        run_id,
        [DirectScrapeRequest(url="https://example.com/retry")],
        prior_invocation_id=failed.invocation_id,
        idempotency_key="integration-retry-second",
    )
    assert retried.status == "complete"
    assert retry_adapter.calls == ["https://example.com/retry"]

    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT retry_parent_id FROM extraction_attempts WHERE id=%s",
            (retried.items[0].extraction_attempt_id,),
        )
        assert cur.fetchone()[0] == failed.items[0].extraction_attempt_id
        cur.execute(
            "SELECT parent_invocation_id FROM research_invocations WHERE id=%s",
            (retried.invocation_id,),
        )
        assert cur.fetchone()[0] == failed.invocation_id


def test_failed_retry_preserves_attempt_and_invocation_lineage(tmp_path: Path):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)
    request = [DirectScrapeRequest(url="https://example.com/retry-lineage")]

    first_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(
                raw_payload=b"",
                returncode=1,
                stderr=b"initial upstream failure",
                metadata={"failure_class": "network"},
            )
        ]
    )
    first = _build_service(tmp_path, lambda: first_adapter).execute(
        run_id,
        request,
        idempotency_key="retry-lineage-first",
    )
    assert first.status == "failed"

    second_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(
                raw_payload=b"",
                returncode=1,
                stderr=b"retry also failed",
                metadata={"failure_class": "network"},
            )
        ]
    )
    second = _build_service(tmp_path, lambda: second_adapter).retry_failed(
        run_id,
        request,
        prior_invocation_id=first.invocation_id,
        idempotency_key="retry-lineage-second",
    )
    assert second.status == "failed"

    third_adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(
                raw_payload=b"# Third attempt\n\nAuthoritative success.",
                provider_request_id="provider-third",
                metadata={"firecrawl_cli_version": "test-cli"},
            )
        ]
    )
    third = _build_service(tmp_path, lambda: third_adapter).retry_failed(
        run_id,
        request,
        prior_invocation_id=second.invocation_id,
        idempotency_key="retry-lineage-third",
    )
    assert third.status == "complete"

    first_attempt = first.items[0].extraction_attempt_id
    second_attempt = second.items[0].extraction_attempt_id
    third_attempt = third.items[0].extraction_attempt_id
    assert first_attempt is not None
    assert second_attempt is not None
    assert third_attempt is not None

    inspector = InspectionService(_config(tmp_path))
    attempts = inspector.list_extraction_attempts(
        candidate_id=first.items[0].candidate_id
    )
    by_id = {item["id"]: item for item in attempts["items"]}
    assert by_id[str(first_attempt)]["retry_parent_id"] is None
    assert by_id[str(second_attempt)]["retry_parent_id"] == str(first_attempt)
    assert by_id[str(third_attempt)]["retry_parent_id"] == str(second_attempt)

    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT parent_invocation_id FROM research_invocations WHERE id=%s",
            (second.invocation_id,),
        )
        assert cur.fetchone()[0] == first.invocation_id
        cur.execute(
            "SELECT parent_invocation_id FROM research_invocations WHERE id=%s",
            (third.invocation_id,),
        )
        assert cur.fetchone()[0] == second.invocation_id


def test_same_snapshot_bytes_do_not_collapse_parser_document_identity(
    tmp_path: Path,
):
    _integration()
    run_id = uuid4()
    _insert_run(run_id)
    payload = b"identical textual bytes"

    markdown_adapter = _SequenceAdapter([ScrapeTransportResult(raw_payload=payload)])
    markdown = _build_service(tmp_path, lambda: markdown_adapter).execute(
        run_id,
        [
            DirectScrapeRequest(
                url="https://example.com/same-bytes",
                format="markdown",
            )
        ],
        idempotency_key="same-bytes-markdown",
    )

    summary_adapter = _SequenceAdapter([ScrapeTransportResult(raw_payload=payload)])
    summary = _build_service(tmp_path, lambda: summary_adapter).execute(
        run_id,
        [
            DirectScrapeRequest(
                url="https://example.com/same-bytes",
                format="summary",
            )
        ],
        idempotency_key="same-bytes-summary",
    )

    assert summary.items[0].snapshot_id == markdown.items[0].snapshot_id
    assert summary.items[0].document_id != markdown.items[0].document_id
    assert summary.items[0].chunk_ids != markdown.items[0].chunk_ids
    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """SELECT id,parser_name FROM documents
            WHERE id IN (%s,%s) ORDER BY parser_name""",
            (markdown.items[0].document_id, summary.items[0].document_id),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0][1] != rows[1][1]


def test_persist_ingest_parser_name_is_additive_and_trailing():
    import inspect

    parameters = list(
        inspect.signature(PostgresUnitOfWork.persist_ingest).parameters.values()
    )
    names = [parameter.name for parameter in parameters]
    assert names[-5:] == [
        "parser_version",
        "chunker_version",
        "normalization_version",
        "chunker_name",
        "parser_name",
    ]
    assert parameters[-1].default == "markdown"


def test_ingest_batch_uses_prepared_ingest_named_contract(monkeypatch):
    class Prepared:
        called = False

        def __iter__(self):
            raise AssertionError("PreparedIngest must not be unpacked positionally")

        def persist_args(self):
            self.called = True
            return ("prepared-contract",)

    prepared = Prepared()

    class Savepoint:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    class Uow:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def start_ingestion_batch(self, *_args):
            return uuid4()

        def savepoint(self):
            return Savepoint()

        def persist_ingest(self, *args):
            assert args == ("prepared-contract",)
            return SimpleNamespace(snapshot_id=uuid4(), chunk_ids=())

        def record_batch_asset(self, *_args, **_kwargs):
            return None

        def finish_ingestion_batch(self, *_args):
            return None

        def export_invocation(self, _invocation_id):
            return {"assets": []}

    service = CorpusService(
        StoreConfig.from_env(),
        lambda: Uow(),
        object(),
    )
    monkeypatch.setattr(service, "_prepare_ingest", lambda _request: prepared)

    manifest = service.ingest_batch(
        "typed-contract",
        "scrape",
        [IngestRequest("https://example.com/typed", b"# Typed\n\nContract")],
    )

    assert prepared.called is True
    assert manifest["failure_count"] == 0
