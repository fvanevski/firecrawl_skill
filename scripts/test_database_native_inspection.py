from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from research_store.config import StoreConfig
from research_store.direct_scrape_service import DirectScrapeService
from research_store.inspection_cli import execute, parser
from research_store.inspection_contract import (
    InspectionBoundError,
    InspectionIntegrityError,
    InspectionNotFoundError,
    PageRequest,
    PassageBounds,
    _decode_cursor,
    _encode_cursor,
)
from research_store.inspection_service import InspectionService


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
    values = StoreConfig.from_env().__dict__ | {
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


def test_cursor_round_trip_and_kind_isolation():
    timestamp = datetime(2026, 8, 2, tzinfo=timezone.utc)
    row_id = uuid4()
    encoded = _encode_cursor("runs", timestamp, row_id)
    assert _decode_cursor("runs", encoded) == (timestamp, row_id)
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        _decode_cursor("invocations", encoded)


def test_page_bounds_are_hard():
    with pytest.raises(ValueError, match="between 1 and 100"):
        PageRequest(limit=101)
    with pytest.raises(ValueError, match="max_chars"):
        PassageBounds(max_chars=64_001)
    with pytest.raises(ValueError, match="max_tokens"):
        PassageBounds(max_tokens=16_001)


def test_list_runs_uses_keyset_cursor(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [
        (uuid4(), "fr_one", "one", "created", 0, "agent_led", None, now, None, None),
        (uuid4(), "fr_two", "two", "created", 0, "agent_led", None, now, None, None),
    ]
    inspector, connection = service(tmp_path, [rows])
    result = inspector.list_runs(PageRequest(limit=1))
    assert result["item_count"] == 1
    assert result["truncated"] is True
    assert result["next_cursor"]
    assert (
        "ORDER BY started_at DESC,id DESC" in connection.cursor_value.statements[0][0]
    )


def test_replay_requires_only_response_id_and_verifies_blob(tmp_path):
    response_id = uuid4()
    run_id = uuid4()
    payload = json.dumps({"success": True, "data": {"web": []}}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / digest[:2] / digest[2:4] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    now = datetime.now(timezone.utc)
    response_row = (
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
    inspector, _ = service(tmp_path, [[response_row], []])
    result = inspector.replay_search(response_id)
    assert result["response"]["id"] == str(response_id)
    assert result["payload"]["success"] is True
    assert result["payload_integrity"]["verified"] is True


def test_replay_fails_closed_on_integrity_mismatch(tmp_path):
    response_id = uuid4()
    digest = hashlib.sha256(b"expected").hexdigest()
    path = tmp_path / digest[:2] / digest[2:4] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered")
    now = datetime.now(timezone.utc)
    row = (
        response_id,
        uuid4(),
        "fr_test",
        "q",
        "firecrawl",
        None,
        "succeeded",
        200,
        "v1",
        digest,
        len(b"expected"),
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
    inspector, _ = service(tmp_path, [[row], []])
    with pytest.raises(InspectionIntegrityError):
        inspector.replay_search(response_id)


def test_replay_rejects_oversize_before_blob_read(tmp_path):
    now = datetime.now(timezone.utc)
    digest = "a" * 64
    row = (
        uuid4(),
        uuid4(),
        "fr_test",
        "q",
        "firecrawl",
        None,
        "succeeded",
        200,
        "v1",
        digest,
        11,
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
    inspector, _ = service(tmp_path, [[row], []])
    with pytest.raises(InspectionBoundError, match="exceeds max_bytes"):
        inspector.replay_search(row[0], max_bytes=10)


def test_missing_response_is_typed(tmp_path):
    inspector, _ = service(tmp_path, [[]])
    with pytest.raises(InspectionNotFoundError):
        inspector.replay_search(uuid4())


def test_candidate_scrape_uses_stable_ids_and_preserves_preflight_boundary(tmp_path):
    candidate = uuid4()
    run_id = uuid4()
    calls = []

    class Direct:
        def execute(self, actual_run, requests, *, idempotency_key=None):
            calls.append((actual_run, requests, idempotency_key))
            return SimpleNamespace(to_dict=lambda: {"status": "complete"})

    inspector, _ = service(
        tmp_path,
        [[(candidate, run_id)]],
        direct_scrape_factory=lambda: Direct(),
    )
    result = inspector.scrape_candidates([candidate], idempotency_key="stable-key")
    assert result == {"status": "complete"}
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


def test_passages_are_bounded_and_cursorable(tmp_path):
    rows = [
        (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            0,
            3,
            "a" * 64,
            "abcdef",
        ),
        (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            1,
            3,
            "b" * 64,
            "ghijkl",
        ),
    ]
    inspector, _ = service(tmp_path, [rows])
    result = inspector.passages(
        uuid4(), PassageBounds(limit=2, max_chars=8, max_tokens=10)
    )
    assert result["item_count"] == 2
    assert result["items"][1]["text"] == "gh"
    assert result["items"][1]["text_truncated"] is True
    assert result["truncated"] is True
    assert result["next_cursor"]


def test_cli_routes_database_native_commands_without_paths():
    service = SimpleNamespace(
        replay_search=lambda response_id, max_bytes: {
            "response_id": response_id,
            "max_bytes": max_bytes,
        }
    )
    args = parser().parse_args(["replay-search", str(uuid4()), "--max-bytes", "99"])
    result = execute(args, service)
    assert result["max_bytes"] == 99
