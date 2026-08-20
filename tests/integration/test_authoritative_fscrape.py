from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest import mock
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_workflow_operation_service,
)
from research_store.direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapePersistenceError,
    DirectScrapeRequest,
    DirectScrapeService,
    ScrapeTransportResult,
)
from research_store.fscrape_contract import (
    FScrapeError,
    FScrapeRequest,
    FScrapeResult,
)
from research_store.fscrape_service import (
    FScrapeService,
    ValidatedDirectScrapeService,
    build_fscrape_service,
)
from research_store.postgres import connect

RUN_UUID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = f"fr_{RUN_UUID.hex}"
INVOCATION_UUID = UUID("22222222-2222-4222-8222-222222222222")
EXTERNAL_INVOCATION_ID = "fc_33333333333343338333333333333333"
OTHER_EXTERNAL_INVOCATION_ID = "fc_44444444444444448444444444444444"
TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


def _item(
    index: int,
    *,
    status: str = "succeeded",
    request_format: str = "markdown",
    mime_type: str = "text/markdown",
    chunk_ids: tuple[UUID, ...] = (),
    error: str | None = None,
) -> DirectScrapeItemResult:
    suffix = index + 10
    return DirectScrapeItemResult(
        index=index,
        item_key=f"item-{index}",
        status=status,
        requested_url=f"https://example.com/{index}",
        canonical_url=f"https://example.com/{index}",
        candidate_id=UUID(f"00000000-0000-4000-8000-{suffix:012d}"),
        invocation_id=INVOCATION_UUID,
        format=request_format,
        mime_type=mime_type,
        extraction_attempt_id=(
            UUID(f"10000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        source_id=(
            UUID(f"20000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        snapshot_id=(
            UUID(f"30000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        document_id=(
            UUID(f"40000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        derivation_id=(
            UUID(f"50000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        chunk_ids=chunk_ids,
        content_sha256="a" * 64 if status == "succeeded" else None,
        raw_blob_sha256="b" * 64 if status == "succeeded" else None,
        error=error,
        diagnostic=error,
        failure_class=None if status == "succeeded" else "http_error",
    )


def _batch(
    items: tuple[DirectScrapeItemResult, ...],
    *,
    status: str = "complete",
    idempotency_key: str = "fscrape:test",
    replayed: bool = False,
) -> DirectScrapeBatchResult:
    return DirectScrapeBatchResult(
        run_id=RUN_UUID,
        invocation_id=INVOCATION_UUID,
        idempotency_key=idempotency_key,
        status=status,
        items=items,
        replayed=replayed,
    )


class _Cursor:
    def __init__(self, index_rows, authoritative_external_id):
        self.index_rows = index_rows
        self.authoritative_external_id = authoritative_external_id
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchone(self):
        if "FROM research_invocations" in self.sql:
            return (self.authoritative_external_id,)
        raise AssertionError(f"unexpected fetchone for {self.sql}")

    def fetchall(self):
        if "FROM index_jobs" in self.sql:
            return list(self.index_rows)
        raise AssertionError(f"unexpected fetchall for {self.sql}")


class _UnitOfWork:
    def __init__(self, index_rows, authoritative_external_id):
        self.connection = SimpleNamespace(
            cursor=lambda: _Cursor(index_rows, authoritative_external_id)
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _RunService:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls = []

    def status(self, *, external_id):
        self.calls.append(external_id)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=RUN_UUID)


class _RecordingDirectService:
    def __init__(
        self,
        *,
        batch=None,
        index_rows=(),
        authoritative_external_id=EXTERNAL_INVOCATION_ID,
    ):
        self.batch = batch
        self.index_rows = index_rows
        self.authoritative_external_id = authoritative_external_id
        self.calls = []
        self.uow_factory = lambda: _UnitOfWork(
            self.index_rows, self.authoritative_external_id
        )

    def execute(self, run_id, requests, **kwargs):
        self.calls.append((run_id, tuple(requests), kwargs))
        if self.batch is not None:
            return self.batch
        items = tuple(
            _item(
                index,
                request_format=request.effective_format,
                mime_type=request.effective_mime_type,
            )
            for index, request in enumerate(requests)
        )
        return _batch(items, idempotency_key=kwargs["idempotency_key"])


@pytest.mark.parametrize(
    ("format_name", "summary", "effective_format", "mime_type"),
    [
        ("markdown", False, "markdown", "text/markdown"),
        ("html", False, "html", "text/html"),
        ("rawHtml", False, "rawHtml", "text/html"),
        ("json", False, "json", "application/json"),
        ("links", False, "links", "application/json"),
        ("images", False, "images", "application/json"),
        ("summary", False, "summary", "text/plain"),
        ("markdown", True, "summary", "text/plain"),
    ],
)
def test_each_supported_format_delegates_to_direct_service(
    format_name,
    summary,
    effective_format,
    mime_type,
):
    direct = _RecordingDirectService()
    service = FScrapeService(cast(DirectScrapeService, direct), _RunService())

    result = service.execute(
        FScrapeRequest(
            urls=("https://example.com/report",),
            research_run_id=RUN_ID,
            format=format_name,
            summary=summary,
            external_invocation_id=EXTERNAL_INVOCATION_ID,
        )
    )

    run_id, requests, kwargs = direct.calls[0]
    assert run_id == RUN_UUID
    assert requests[0].effective_format == effective_format
    assert requests[0].effective_mime_type == mime_type
    assert kwargs["external_invocation_id"] == EXTERNAL_INVOCATION_ID
    assert result.external_invocation_id == EXTERNAL_INVOCATION_ID
    assert result.to_dict()["items"][0]["mime_type"] == mime_type


def test_structured_request_is_json_with_schema_provenance():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    direct = _RecordingDirectService()

    FScrapeService(cast(DirectScrapeService, direct), _RunService()).execute(
        FScrapeRequest(
            urls=("https://example.com/structured",),
            research_run_id=RUN_ID,
            schema=schema,
            external_invocation_id=EXTERNAL_INVOCATION_ID,
        )
    )

    request = direct.calls[0][1][0]
    assert request.schema == schema
    assert request.effective_format == "json"
    assert request.effective_mime_type == "application/json"


def test_structured_payload_is_validated_before_ingestion():
    service = object.__new__(ValidatedDirectScrapeService)
    captured = {}

    def persist_failure(self, context, invocation_id, target, transport):
        captured.update(
            context=context,
            invocation_id=invocation_id,
            target=target,
            transport=transport,
        )
        return "failed-item"

    service._persist_failure = MethodType(persist_failure, service)
    target = SimpleNamespace(
        request=DirectScrapeRequest(
            url="https://example.com/structured",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        )
    )
    transport = ScrapeTransportResult(raw_payload=b'{"answer": "wrong"}')

    result = ValidatedDirectScrapeService._persist_success(
        service,
        object(),
        INVOCATION_UUID,
        target,
        transport,
    )

    assert result == "failed-item"
    assert captured["transport"].metadata["failure_class"] == "schema_validation"
    assert captured["transport"].raw_payload == transport.raw_payload
    assert b"schema validation failed" in captured["transport"].stderr


def test_valid_structured_payload_delegates_to_authoritative_ingestion():
    service = object.__new__(ValidatedDirectScrapeService)
    target = SimpleNamespace(
        request=DirectScrapeRequest(
            url="https://example.com/structured",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        )
    )
    transport = ScrapeTransportResult(raw_payload=b'{"answer": 42}')

    with mock.patch.object(
        DirectScrapeService,
        "_persist_success",
        return_value="persisted-item",
    ) as persist:
        result = ValidatedDirectScrapeService._persist_success(
            service,
            object(),
            INVOCATION_UUID,
            target,
            transport,
        )

    assert result == "persisted-item"
    persist.assert_called_once()


def test_multi_url_partial_result_preserves_item_failures_and_stable_ids():
    chunk_id = UUID("60000000-0000-4000-8000-000000000010")
    job_id = UUID("70000000-0000-4000-8000-000000000010")
    direct = _RecordingDirectService(
        batch=_batch(
            (
                _item(0, chunk_ids=(chunk_id,)),
                _item(1, status="failed", error="upstream denied request"),
            ),
            status="partial",
        ),
        index_rows=((job_id, chunk_id),),
    )

    result = FScrapeService(cast(DirectScrapeService, direct), _RunService()).execute(
        FScrapeRequest(
            urls=("https://example.com/0", "https://example.com/1"),
            research_run_id=RUN_ID,
            external_invocation_id=EXTERNAL_INVOCATION_ID,
        )
    )
    payload = result.to_dict()

    assert payload["status"] == "partial"
    assert payload["batch_id"] == str(INVOCATION_UUID)
    assert [item["status"] for item in payload["items"]] == [
        "succeeded",
        "failed",
    ]
    assert payload["items"][0]["source_id"] is not None
    assert payload["items"][0]["index_job_ids"] == [str(job_id)]
    assert payload["items"][1]["error"] == "upstream denied request"
    assert "/tmp/" not in json.dumps(payload)
    assert "output_path" not in json.dumps(payload)


def test_nonexistent_external_run_fails_before_direct_service():
    direct = _RecordingDirectService()
    service = FScrapeService(
        cast(DirectScrapeService, direct), _RunService(error=KeyError(RUN_ID))
    )

    with pytest.raises(FScrapeError, match="research run does not exist") as error:
        service.execute(
            FScrapeRequest(
                urls=("https://example.com/0",),
                research_run_id=RUN_ID,
                external_invocation_id=EXTERNAL_INVOCATION_ID,
            )
        )

    assert error.value.stage == "preflight"
    assert direct.calls == []


def test_missing_committed_index_job_fails_closed():
    chunk_id = UUID("60000000-0000-4000-8000-000000000011")
    direct = _RecordingDirectService(
        batch=_batch((_item(0, chunk_ids=(chunk_id,)),)),
        index_rows=(),
    )

    with pytest.raises(
        DirectScrapePersistenceError,
        match="without index jobs",
    ) as error:
        FScrapeService(cast(DirectScrapeService, direct), _RunService()).execute(
            FScrapeRequest(
                urls=("https://example.com/0",),
                research_run_id=RUN_ID,
                external_invocation_id=EXTERNAL_INVOCATION_ID,
            )
        )
    assert error.value.stage == "indexing"


def test_result_uses_committed_external_identity_on_explicit_key_replay():
    direct = _RecordingDirectService(
        batch=_batch((_item(0),), replayed=True),
        authoritative_external_id=EXTERNAL_INVOCATION_ID,
    )

    result = FScrapeService(cast(DirectScrapeService, direct), _RunService()).execute(
        FScrapeRequest(
            urls=("https://example.com/replay",),
            research_run_id=RUN_ID,
            external_invocation_id=OTHER_EXTERNAL_INVOCATION_ID,
            idempotency_key="caller-key",
        )
    )

    assert direct.calls[0][2]["external_invocation_id"] == OTHER_EXTERNAL_INVOCATION_ID
    assert result.external_invocation_id == EXTERNAL_INVOCATION_ID
    assert result.to_dict()["external_invocation_id"] == EXTERNAL_INVOCATION_ID


def test_missing_authoritative_external_identity_fails_closed():
    direct = _RecordingDirectService(authoritative_external_id=None)

    with pytest.raises(
        DirectScrapePersistenceError,
        match="has no external identity",
    ):
        FScrapeService(cast(DirectScrapeService, direct), _RunService()).execute(
            FScrapeRequest(
                urls=("https://example.com/missing-identity",),
                research_run_id=RUN_ID,
                external_invocation_id=EXTERNAL_INVOCATION_ID,
            )
        )


def test_default_idempotency_is_invocation_scoped_and_explicit_key_is_preserved():
    direct = _RecordingDirectService()
    service = FScrapeService(cast(DirectScrapeService, direct), _RunService())
    base = {
        "urls": ("https://example.com/replay",),
        "research_run_id": RUN_ID,
    }

    service.execute(
        FScrapeRequest(**base, external_invocation_id=EXTERNAL_INVOCATION_ID)
    )
    first_key = direct.calls[-1][2]["idempotency_key"]
    service.execute(
        FScrapeRequest(**base, external_invocation_id=EXTERNAL_INVOCATION_ID)
    )
    assert direct.calls[-1][2]["idempotency_key"] == first_key

    service.execute(
        FScrapeRequest(
            **base,
            external_invocation_id=OTHER_EXTERNAL_INVOCATION_ID,
        )
    )
    assert direct.calls[-1][2]["idempotency_key"] != first_key

    service.execute(
        FScrapeRequest(
            **base,
            external_invocation_id=EXTERNAL_INVOCATION_ID,
            idempotency_key="caller-key",
        )
    )
    assert direct.calls[-1][2]["idempotency_key"] == "caller-key"


def test_result_lists_are_bounded_and_schema_versioned():
    items = tuple(_item(index) for index in range(105))
    result = FScrapeResult(
        research_run_id=RUN_ID,
        external_invocation_id=EXTERNAL_INVOCATION_ID,
        batch=_batch(items),
        index_job_ids_by_chunk={},
    ).to_dict()

    assert result["schema_version"] == "authoritative-fscrape-v1"
    assert "schem_version" not in result
    assert result["item_count"] == 105
    assert len(result["items"]) == 100
    assert result["items_truncated"] is True


class _SequenceAdapter:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def scrape(self, url, **_kwargs):
        self.calls.append(url)
        return next(self.outcomes)


@pytest.mark.skipif(
    not TEST_DSN,
    reason="requires explicit disposable PostgreSQL test DSN",
)
def test_postgres_wrapper_persists_structured_outcomes_and_replays_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp_root))
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    run_external_id = f"fr_{uuid4().hex}"
    invocation_external_id = f"fc_{uuid4().hex}"
    replay_requested_external_id = f"fc_{uuid4().hex}"
    idempotency_key = f"fscrape-integration:{uuid4()}"
    run_service = build_run_service(config)
    run_service.create(
        objective="authoritative fscrape integration",
        external_id=run_external_id,
    )
    build_workflow_operation_service(config).prepare_run(run_external_id)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    adapter = _SequenceAdapter(
        [
            ScrapeTransportResult(raw_payload=b'{"answer": 42}'),
            ScrapeTransportResult(raw_payload=b'{"answer": "wrong"}'),
        ]
    )
    service = build_fscrape_service(config, adapter_factory=lambda: adapter)
    request = FScrapeRequest(
        urls=(
            "https://example.com/valid-structured",
            "https://example.com/invalid-structured",
        ),
        research_run_id=run_external_id,
        schema=schema,
        idempotency_key=idempotency_key,
        external_invocation_id=invocation_external_id,
    )

    result = service.execute(request)

    assert result.status == "partial"
    assert result.external_invocation_id == invocation_external_id
    assert [item.status for item in result.batch.items] == ["succeeded", "failed"]
    assert result.batch.items[0].mime_type == "application/json"
    assert result.batch.items[0].chunk_ids
    assert result.batch.items[1].failure_class == "schema_validation"
    assert result.batch.items[1].extraction_attempt_id is not None
    assert result.to_dict()["items"][0]["index_job_ids"]
    assert adapter.calls == [
        "https://example.com/valid-structured",
        "https://example.com/invalid-structured",
    ]

    with connect(TEST_DSN) as connection, connection.cursor() as cur:
        cur.execute(
            """SELECT external_invocation_id,status
            FROM research_invocations WHERE id=%s AND run_id=%s""",
            (result.batch.invocation_id, result.batch.run_id),
        )
        assert cur.fetchone() == (invocation_external_id, "partial")
        attempts: list[Any] = []
        for item in result.batch.items:
            cur.execute(
                """SELECT exit_status,failure_class,raw_blob_sha256,
                normalized_blob_sha256
                FROM extraction_attempts WHERE id=%s""",
                (item.extraction_attempt_id,),
            )
            attempts.append(cur.fetchone())
        cur.execute(
            "SELECT count(*) FROM research_invocations WHERE run_id=%s",
            (result.batch.run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1

    assert attempts[0][0:2] == ("succeeded", "none")
    assert attempts[0][2] is not None
    assert attempts[0][3] is not None
    assert attempts[1][0:2] == ("failed", "schema_validation")
    assert attempts[1][2] is not None
    assert attempts[1][3] is None
    assert any(path.is_file() for path in config.blob_root.rglob("*"))

    replay_adapter = _SequenceAdapter([])
    replay_service = build_fscrape_service(
        config, adapter_factory=lambda: replay_adapter
    )
    replayed = replay_service.execute(
        replace(
            request,
            external_invocation_id=replay_requested_external_id,
        )
    )

    assert replayed.batch.replayed is True
    assert replayed.batch.invocation_id == result.batch.invocation_id
    assert replayed.external_invocation_id == invocation_external_id
    assert replay_adapter.calls == []
    assert list(temp_root.iterdir()) == []
