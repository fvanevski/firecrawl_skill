from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store import inspection_cli
from firecrawl_skill.research_store.acquisition.direct_scrape_application import (
    DirectScrapeService,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.inspection_cli import execute, parser
from firecrawl_skill.research_store.inspection_contract import (
    InspectionBoundError,
    InspectionIntegrityError,
    InspectionNotFoundError,
    PageRequest,
    PassageBounds,
    _bounded_json,
    _decode_cursor,
    _encode_cursor,
    _scope_fingerprint,
)
from firecrawl_skill.research_store.inspection_service import InspectionService


class FakeCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.current = []
        self.statements = []

    def execute(self, statement, params=()):
        self.statements.append((" ".join(statement.split()), tuple(params)))
        self.current = next(self.responses)

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, responses):
        self.cursor_value = FakeCursor(responses)

    def cursor(self):
        return self.cursor_value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def config(tmp_path: Path) -> StoreConfig:
    values: dict[str, Any] = StoreConfig.from_env().__dict__ | {
        "database_url": "postgresql://test/test",
        "blob_root": tmp_path,
    }
    return StoreConfig(**values)


def service(tmp_path, responses, **kwargs):
    connection = FakeConnection(responses)
    return (
        InspectionService(
            config(tmp_path),
            connection_factory=lambda: connection,
            **kwargs,
        ),
        connection,
    )


def test_cursor_round_trip_and_scope_isolation():
    timestamp = datetime(2026, 8, 2, tzinfo=timezone.utc)
    row_id = uuid4()
    scope = _scope_fingerprint("runs", tenant="one")
    encoded = _encode_cursor("runs", timestamp, row_id, scope=scope)
    assert _decode_cursor("runs", encoded, scope=scope) == (timestamp, row_id)
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        _decode_cursor(
            "runs",
            encoded,
            scope=_scope_fingerprint("runs", tenant="two"),
        )
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        _decode_cursor("invocations", encoded, scope=scope)


def test_page_bounds_are_hard():
    with pytest.raises(ValueError, match="between 1 and 100"):
        PageRequest(limit=101)
    with pytest.raises(ValueError, match="max_chars"):
        PassageBounds(max_chars=64_001)
    with pytest.raises(ValueError, match="max_tokens"):
        PassageBounds(max_tokens=16_001)


def test_nested_json_is_bounded():
    value = _bounded_json({"payload": "x" * 20_000}, limit=100)
    assert value["truncated"] is True
    assert value["original_char_count"] > 100
    assert len(value["preview"]) == 100


def test_list_runs_uses_keyset_cursor(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [
        (
            uuid4(),
            "fr_one",
            "one",
            "created",
            0,
            "agent_led",
            None,
            now,
            None,
            None,
            False,
        ),
        (
            uuid4(),
            "fr_two",
            "two",
            "created",
            0,
            "agent_led",
            None,
            now,
            None,
            None,
            False,
        ),
    ]
    inspector, connection = service(tmp_path, [rows])
    result = inspector.list_runs(PageRequest(limit=1))
    assert result["item_count"] == 1
    assert result["truncated"] is True
    assert result["next_cursor"]
    assert result["items"][0]["temporal_gap_pending"] is False
    assert (
        result["output_bounds"]["serialized_chars"]
        <= result["output_bounds"]["max_serialized_chars"]
    )
    assert (
        "ORDER BY started_at DESC,id DESC" in connection.cursor_value.statements[0][0]
    )


def _response_row(response_id, run_id, payload, digest, now):
    return (
        response_id,
        run_id,
        "fr_test",
        "query",
        "firecrawl",
        None,
        "succeeded",
        200,
        "firecrawl-search-v1",
        digest,
        len(payload),
        "application/json",
        digest,
        0,
        None,
        {},
        {},
        "key",
        now,
        now,
        now,
        None,
    )


def test_replay_requires_only_response_id_and_verifies_blob(tmp_path):
    response_id = uuid4()
    run_id = uuid4()
    payload = json.dumps({"success": True, "data": {"web": []}}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / digest[:2] / digest[2:4] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    inspector, _ = service(
        tmp_path,
        [
            [
                _response_row(
                    response_id,
                    run_id,
                    payload,
                    digest,
                    datetime.now(timezone.utc),
                )
            ],
            [],
        ],
    )
    result = inspector.replay_search(response_id)
    assert result["response"]["id"] == str(response_id)
    assert result["payload"]["success"] is True
    assert result["payload_integrity"]["verified"] is True


def test_replay_fails_closed_on_integrity_mismatch(tmp_path):
    response_id = uuid4()
    payload = b"expected"
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / digest[:2] / digest[2:4] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered")
    inspector, _ = service(
        tmp_path,
        [
            [
                _response_row(
                    response_id,
                    uuid4(),
                    payload,
                    digest,
                    datetime.now(timezone.utc),
                )
            ],
            [],
        ],
    )
    with pytest.raises(InspectionIntegrityError):
        inspector.replay_search(response_id)


def test_replay_rejects_oversize_before_blob_read(tmp_path):
    payload = b"expected-data"
    digest = hashlib.sha256(payload).hexdigest()
    row = _response_row(uuid4(), uuid4(), payload, digest, datetime.now(timezone.utc))
    inspector, _ = service(tmp_path, [[row], []])
    with pytest.raises(InspectionBoundError, match="exceeds max_bytes"):
        inspector.replay_search(row[0], max_bytes=10)


def test_missing_response_is_typed(tmp_path):
    inspector, _ = service(tmp_path, [[]])
    with pytest.raises(InspectionNotFoundError):
        inspector.replay_search(uuid4())


def test_candidate_scrape_uses_stable_ids_and_bounds_result(tmp_path):
    candidate = uuid4()
    run_id = uuid4()
    invocation_id = uuid4()
    calls = []

    class Direct:
        def execute(self, actual_run, requests, *, idempotency_key=None):
            calls.append((actual_run, requests, idempotency_key))
            return SimpleNamespace(
                to_dict=lambda: {
                    "run_id": str(actual_run),
                    "invocation_id": str(invocation_id),
                    "idempotency_key": idempotency_key,
                    "status": "complete",
                    "replayed": False,
                    "items": [
                        {
                            "candidate_id": str(candidate),
                            "status": "succeeded",
                            "chunk_ids": [str(uuid4()) for _ in range(150)],
                        }
                    ],
                }
            )

    inspector, _ = service(
        tmp_path,
        [[(candidate, run_id)]],
        direct_scrape_factory=lambda: Direct(),
    )
    result = inspector.scrape_candidates([candidate], idempotency_key="stable-key")
    assert result["kind"] == "candidate_scrape"
    assert result["status"] == "complete"
    assert result["items"][0]["chunk_ids"]["returned_count"] == 100
    assert result["items"][0]["chunk_ids"]["truncated"] is True
    assert calls[0][0] == run_id
    assert calls[0][1][0].candidate_id == candidate
    assert calls[0][1][0].url is None


def test_failed_authoritative_preflight_never_constructs_adapter(tmp_path):
    candidate = uuid4()
    run_id = uuid4()
    adapter_constructed = False

    def adapter_factory():
        nonlocal adapter_constructed
        adapter_constructed = True
        raise AssertionError("adapter must not be constructed")

    def failed_preflight(**_):
        raise RuntimeError("authoritative preflight failed")

    direct = DirectScrapeService(
        config(tmp_path),
        lambda: None,
        None,
        None,
        adapter_factory=adapter_factory,
        preflight=failed_preflight,
        authority_check=lambda _: None,
    )
    inspector, _ = service(
        tmp_path,
        [[(candidate, run_id)]],
        direct_scrape_factory=lambda: direct,
    )
    with pytest.raises(RuntimeError, match="authoritative preflight failed"):
        inspector.scrape_candidates([candidate])
    assert adapter_constructed is False


def test_candidate_lookup_failure_never_constructs_direct_service(tmp_path):
    constructed = False

    def factory():
        nonlocal constructed
        constructed = True
        raise AssertionError("must not construct")

    inspector, _ = service(tmp_path, [[]], direct_scrape_factory=factory)
    with pytest.raises(InspectionNotFoundError):
        inspector.scrape_candidates([uuid4()])
    assert constructed is False


def _passage_row(chunk_id, document_id, text, ordinal=0):
    return (
        chunk_id,
        document_id,
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        ordinal,
        len(text),
        hashlib.sha256(text.encode()).hexdigest(),
        "bpe_fake",
        text,
    )


def test_passage_cursor_resumes_inside_clipped_chunk_without_loss(tmp_path):
    asset_id = uuid4()
    chunk_id = uuid4()
    document_id = uuid4()
    row = _passage_row(chunk_id, document_id, "abcdefghij")
    inspector, _ = service(tmp_path, [[row], [row], [row], [row]])
    cursor = None
    parts = []
    for _ in range(4):
        page = inspector.passages(
            asset_id,
            PassageBounds(limit=1, max_chars=3, max_tokens=100, cursor=cursor),
        )
        parts.append(page["items"][0]["text"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert "".join(parts) == "abcdefghij"
    assert cursor is None


def test_passage_cursor_cannot_cross_asset_scope(tmp_path):
    chunk_id = uuid4()
    document_id = uuid4()
    row = _passage_row(chunk_id, document_id, "abcdef")
    inspector, _ = service(tmp_path, [[row]])
    first = inspector.passages(
        uuid4(), PassageBounds(limit=1, max_chars=2, max_tokens=100)
    )
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        inspector.passages(
            uuid4(),
            PassageBounds(
                limit=1,
                max_chars=2,
                max_tokens=100,
                cursor=first["next_cursor"],
            ),
        )


def test_missing_candidate_attempt_scope_is_typed(tmp_path):
    inspector, _ = service(tmp_path, [[]])
    with pytest.raises(InspectionNotFoundError, match="candidate not found"):
        inspector.list_extraction_attempts(candidate_id=uuid4())


def test_attempt_history_uses_invocation_result_for_reused_corpus(tmp_path):
    candidate = uuid4()
    run_id = uuid4()
    invocation_id = uuid4()
    attempt_id = uuid4()
    source_id = uuid4()
    snapshot_id = uuid4()
    document_id = uuid4()
    derivation_id = uuid4()
    chunk_ids = [uuid4(), uuid4()]
    now = datetime.now(timezone.utc)
    attempt_row = (
        attempt_id,
        run_id,
        "fr_test",
        invocation_id,
        "fc_test",
        candidate,
        2,
        "firecrawl_main_content",
        "v1",
        "markdown",
        now,
        now,
        "succeeded",
        200,
        "complete",
        "a" * 64,
        10,
        "text/markdown",
        "b" * 64,
        10,
        "text/markdown",
        "parser",
        "none",
        None,
        "acceptable",
        None,
        True,
        None,
        None,
        None,
        None,
        [],
        0,
        now,
    )
    invocation_output = {
        "items": [
            {
                "extraction_attempt_id": str(attempt_id),
                "source_id": str(source_id),
                "snapshot_id": str(snapshot_id),
                "document_id": str(document_id),
                "derivation_id": str(derivation_id),
                "chunk_ids": [str(value) for value in chunk_ids],
            }
        ]
    }
    inspector, _ = service(
        tmp_path,
        [
            [(run_id, "fr_test")],
            [attempt_row],
            [("succeeded", "none", 1)],
            [(invocation_id, invocation_output)],
        ],
    )
    result = inspector.list_extraction_attempts(candidate_id=candidate)
    item = result["items"][0]
    assert item["snapshot_id"] == str(snapshot_id)
    assert item["document_id"] == str(document_id)
    assert item["derivation_id"] == str(derivation_id)
    assert item["chunk_ids"]["items"] == [str(value) for value in chunk_ids]
    assert result["attempt_census"] == {
        "attempted": 1,
        "succeeded": 1,
        "unsuccessful": 0,
        "failure_counts": {},
    }


def test_retry_routes_to_retry_failed_with_prior_lineage(tmp_path):
    prior = uuid4()
    run_id = uuid4()
    candidate = uuid4()
    retried = []
    stored_input = {
        "requests": [
            {
                "url": None,
                "candidate_id": str(candidate),
                "format": "markdown",
                "summary": False,
                "schema": None,
                "mime_type": "text/markdown",
                "options": {},
            }
        ]
    }

    class Direct:
        def retry_failed(self, actual_run, requests, **kwargs):
            retried.append((actual_run, requests, kwargs))
            return SimpleNamespace(
                to_dict=lambda: {
                    "run_id": str(actual_run),
                    "invocation_id": str(uuid4()),
                    "idempotency_key": kwargs["idempotency_key"],
                    "status": "complete",
                    "replayed": False,
                    "items": [],
                }
            )

    inspector, _ = service(
        tmp_path,
        [[(run_id, stored_input)]],
        direct_scrape_factory=lambda: Direct(),
    )
    result = inspector.retry_candidates(prior, idempotency_key="retry-key")
    assert result["kind"] == "candidate_retry"
    assert retried[0][0] == run_id
    assert retried[0][1][0].candidate_id == candidate
    assert retried[0][2]["prior_invocation_id"] == prior


def test_cli_nonzero_for_partial_or_failed_acquisition(monkeypatch, capsys):
    class FakeService:
        def scrape_candidates(self, *_args, **_kwargs):
            return {"kind": "candidate_scrape", "status": "partial"}

    monkeypatch.setattr(inspection_cli, "build_inspection_service", FakeService)
    code = inspection_cli.main(["scrape-candidates", str(uuid4())])
    assert code == 5
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_cli_complete_acquisition_returns_zero(monkeypatch):
    class FakeService:
        def scrape_candidates(self, *_args, **_kwargs):
            return {"kind": "candidate_scrape", "status": "complete"}

    monkeypatch.setattr(inspection_cli, "build_inspection_service", FakeService)
    assert inspection_cli.main(["scrape-candidates", str(uuid4())]) == 0


def test_cli_routes_retry_and_pattern_commands():
    prior = uuid4()
    service = SimpleNamespace(
        retry_candidates=lambda invocation_id, idempotency_key: {
            "invocation_id": invocation_id,
            "idempotency_key": idempotency_key,
        },
        pattern_search=lambda pattern, mode, run, bounds: {
            "pattern": pattern,
            "mode": mode,
        },
    )
    retry_args = parser().parse_args(
        ["retry-candidates", str(prior), "--idempotency-key", "retry-key"]
    )
    retry_result = execute(retry_args, service)
    assert retry_result["invocation_id"] == str(prior)
    pattern_args = parser().parse_args(["pattern-search", "foo|bar", "--mode", "regex"])
    assert execute(pattern_args, service) == {"pattern": "foo|bar", "mode": "regex"}
