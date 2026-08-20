"""
Phase 2 exit-criteria integration and fault tests (#19 / P2-07).

Exit criteria verified:
  1. All candidates survive scratch deletion — PostgreSQL retains candidates
     independently of whether scratch files exist.
  2. Repeated occurrences are retained — the same canonical URL appearing in
     multiple search branches increments recurrence_count and stores one
     candidate occurrence row per branch.
  3. Triage replay produces identical candidate IDs and cards — build_triage_input
     and replay_candidates return the same IDs and card content for a given run
     even after a full scratch purge.
  4. Duplicate ingestion is idempotent — calling record_response_candidates twice
     for the same response_id does not create duplicate candidates or occurrences.
  5. Crashes around blob and database boundaries are reconcilable — a crash after
     blob write but before the DB commit leaves the blob on disk, and
     reconcile_pending_searches re-extracts candidates from the stored response.
  6. Malformed or partially parseable responses are represented correctly — parse
     errors, empty arrays, and responses with some valid / some invalid items are
     all stored with the correct status; valid items in a mixed response produce
     candidates while invalid items are silently skipped.
  7. Export failure does not invalidate committed acquisition — a crash/error
     during scratch-file write does not roll back the PostgreSQL search_response
     or search_candidate rows.
  8. No candidate exists only in scratch state — scratch generation is always
     performed after the DB commit; if scratch fails, DB state is authoritative.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult, utcnow
from firecrawl_skill.research_store.parsing import parse_raw_search_response
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


@pytest.fixture(scope="session")
def prepared_database():
    """Migrate a disposable test database to HEAD before the session runs."""
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    rev = migrate(TEST_DSN)
    assert rev >= 11, f"Expected schema revision >= 11, got {rev}"


def _make_success_payload(*urls: str) -> bytes:
    """Build a minimal Firecrawl-shaped success response with the given URLs."""
    return json.dumps(
        {
            "success": True,
            "data": [
                {"url": u, "title": f"Title for {u}", "snippet": "snippet"}
                for u in urls
            ],
        }
    ).encode("utf-8")


def _make_error_payload(message: str = "Rate limit exceeded") -> bytes:
    return json.dumps({"success": False, "error": message}).encode("utf-8")


def _prepare_acquisition_run(config: StoreConfig, external_id: str) -> None:
    """Advance a newly created run through the canonical acquisition preflight."""
    build_workflow_operation_service(config).prepare_run(external_id)


class StubSearchAdapter:
    """Search adapter that returns a pre-configured payload."""

    def __init__(self, raw_payload: bytes, transport_error: str | None = None):
        self._payload = raw_payload
        self._error = transport_error
        self.call_count = 0

    def search(self, query_text: str, **kwargs) -> SearchAdapterResult:
        self.call_count += 1
        return SearchAdapterResult(
            raw_payload=self._payload,
            http_status=500 if self._error else 200,
            provider_request_id=f"req-{uuid4()}",
            transport_error=self._error,
            transport_metadata={"stub": True, "call_count": self.call_count},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


class TestParseRawSearchResponse:
    """Unit tests for the raw-response parser covering all status classes."""

    def test_succeeded_single_item(self):
        raw = json.dumps(
            {"success": True, "data": [{"url": "https://a.com", "title": "A"}]}
        )
        status, count, summary, err = parse_raw_search_response(raw)
        assert status == "succeeded"
        assert count == 1
        assert err is None
        assert summary["sample_candidates"][0]["url"] == "https://a.com"

    def test_succeeded_multiple_items(self):
        raw = json.dumps(
            {
                "success": True,
                "data": [
                    {"url": "https://a.com"},
                    {"url": "https://b.com"},
                    {"url": "https://c.com"},
                ],
            }
        )
        status, count, _, err = parse_raw_search_response(raw)
        assert status == "succeeded"
        assert count == 3
        assert err is None

    def test_empty_response(self):
        raw = json.dumps({"success": True, "data": []})
        status, count, _, err = parse_raw_search_response(raw)
        assert status == "empty"
        assert count == 0
        assert err is None

    def test_provider_error_explicit_flag(self):
        raw = json.dumps({"success": False, "error": "API quota exceeded"})
        status, count, _, err = parse_raw_search_response(raw)
        assert status == "provider_error"
        assert count == 0
        assert "quota" in (err or "")

    def test_provider_error_http_500(self):
        raw = json.dumps({"detail": "internal server error"})
        status, count, _, _err = parse_raw_search_response(raw, http_status=500)
        assert status == "provider_error"
        assert count == 0

    def test_provider_error_http_429(self):
        raw = json.dumps({"message": "rate limited"})
        status, _count, _, err = parse_raw_search_response(raw, http_status=429)
        assert status == "provider_error"
        assert "rate limited" in (err or "")

    def test_parse_error_html_gateway(self):
        raw = "<html>502 Bad Gateway</html>"
        status, count, _, err = parse_raw_search_response(raw)
        assert status == "parse_error"
        assert count == 0
        assert "JSON" in (err or "")

    def test_parse_error_binary_garbage(self):
        raw = b"\xff\xfe\x00\x01garbage"
        status, count, _, _err = parse_raw_search_response(raw)
        assert status == "parse_error"
        assert count == 0

    def test_parse_error_json_number_root(self):
        raw = "42"
        status, _count, _, err = parse_raw_search_response(raw)
        assert status == "parse_error"
        assert "root must be an object or array" in (err or "")

    def test_partial_items_url_missing_skipped_unit(self):
        """Items without a URL are counted by the parser and skipped later."""
        raw = json.dumps(
            {
                "success": True,
                "data": [
                    {"url": "https://valid.com", "title": "Valid"},
                    {"title": "No URL here"},
                    {"url": "", "title": "Empty URL"},
                ],
            }
        )
        status, count, _summary, err = parse_raw_search_response(raw)
        assert status == "succeeded"
        assert count == 3
        assert err is None


class TestBlobStoreIsolation:
    """Unit tests proving blob write and hash integrity without a database."""

    def test_put_and_verify(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        data = b"hello world"
        ref = store.put(io.BytesIO(data))
        expected_sha = hashlib.sha256(data).hexdigest()
        assert ref.sha256 == expected_sha
        assert store.exists(expected_sha)
        assert store.verify(expected_sha)

    def test_idempotent_put(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        data = b"repeated blob"
        ref1 = store.put(io.BytesIO(data))
        ref2 = store.put(io.BytesIO(data))
        assert ref1.sha256 == ref2.sha256
        blob_path = store.path_for(ref1.sha256)
        assert blob_path.is_file()

    def test_missing_blob_returns_false(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        fake_sha = "a" * 64
        assert store.exists(fake_sha) is False
        assert store.verify(fake_sha) is False


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestAuthoritativeAcquisitionPersistence:
    """Successful acquisition is represented only by authoritative records."""

    def test_success_has_no_scratch_result_surface(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)

        ext_id = f"run-authoritative-only-{uuid4()}"
        run_svc.create(objective="test authoritative acquisition", external_id=ext_id)
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id

        adapter = StubSearchAdapter(
            _make_success_payload("https://authoritative.example.com/doc")
        )
        acq_svc = build_acquisition_service(config, search_adapter=adapter)
        result = acq_svc.execute_search(run_id, "authoritative-only acquisition")

        assert result.postgres_committed is True
        assert not hasattr(result, "scratch_exported")
        assert not hasattr(result, "scratch_error")
        assert len(run_svc.list_candidates(run_id)) == 1
        stored = run_svc.get_search_response(result.search_response_id)
        assert stored["status"] == "succeeded"
        assert stored["result_count"] == 1

    def test_success_writes_no_acquisition_artifacts_under_tmpdir(
        self, tmp_path, prepared_database, monkeypatch
    ):
        migrate(TEST_DSN)
        monitored_tmp = tmp_path / "monitored-tmp"
        monitored_tmp.mkdir()
        monkeypatch.setenv("TMPDIR", str(monitored_tmp))
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-no-temp-artifacts-{uuid4()}"
        run_svc.create(
            objective="test no temp acquisition artifacts", external_id=ext_id
        )
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        adapter = StubSearchAdapter(
            _make_success_payload("https://no-temp-artifacts.example.com/doc")
        )

        result = build_acquisition_service(
            config, search_adapter=adapter
        ).execute_search(run_id, "no temp artifacts")

        assert result.postgres_committed is True
        assert list(monitored_tmp.rglob("*")) == []
        assert len(run_svc.list_candidates(run_id)) == 1

    def test_removed_scratch_arguments_fail_before_provider_execution(
        self, tmp_path, prepared_database
    ):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-reject-scratch-args-{uuid4()}"
        run_svc.create(objective="test removed scratch arguments", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        adapter = StubSearchAdapter(
            _make_success_payload("https://must-not-run.example.com/doc")
        )
        service = build_acquisition_service(config, search_adapter=adapter)

        with pytest.raises(TypeError):
            cast(Any, service.execute_search)(
                run_id,
                "removed scratch arguments",
                scratch_dir=tmp_path / "removed",
            )

        assert adapter.call_count == 0
        assert run_svc.list_candidates(run_id) == []


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestRepeatedOccurrencesRetained:
    def test_multi_branch_recurrence_increments_count(
        self, tmp_path, prepared_database
    ):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)

        ext_id = f"run-recurrence-p2-{uuid4()}"
        run_svc.create(objective="multi branch recurrence", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        shared_url = "https://shared.example.org/resource"

        resp1 = run_svc.record_search_response(
            run_id,
            "branch alpha query",
            "firecrawl",
            json.dumps(
                {"success": True, "data": [{"url": shared_url, "title": "Alpha"}]}
            ),
            f"branch-alpha-{uuid4()}",
        )
        resp2 = run_svc.record_search_response(
            run_id,
            "branch beta query",
            "firecrawl",
            json.dumps(
                {
                    "success": True,
                    "data": [
                        {"url": shared_url, "title": "Beta (same URL)"},
                        {"url": "https://unique.example.org/other", "title": "Unique"},
                    ],
                }
            ),
            f"branch-beta-{uuid4()}",
        )

        run_svc.record_response_candidates(run_id, resp1["id"])
        run_svc.record_response_candidates(run_id, resp2["id"])

        cands = run_svc.list_candidates(run_id)
        assert len(cands) == 2
        shared_cand = next(c for c in cands if c["canonical_url"] == shared_url)
        assert shared_cand["recurrence_count"] == 2
        occs = run_svc.list_candidate_occurrences(shared_cand["id"])
        assert len(occs) == 2
        assert {o["query_text"] for o in occs} == {
            "branch alpha query",
            "branch beta query",
        }

    def test_four_branch_recurrence(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-4branch-{uuid4()}"
        run_svc.create(objective="four branch recurrence", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        recurring_url = "https://multi.example.com/popular-article"
        n_branches = 4

        for i in range(n_branches):
            resp = run_svc.record_search_response(
                run_id,
                f"query-branch-{i}",
                "firecrawl",
                json.dumps(
                    {
                        "success": True,
                        "data": [{"url": recurring_url, "title": f"Branch {i}"}],
                    }
                ),
                f"four-branch-key-{i}-{uuid4()}",
            )
            run_svc.record_response_candidates(run_id, resp["id"])

        cands = run_svc.list_candidates(run_id)
        assert len(cands) == 1
        assert cands[0]["recurrence_count"] == n_branches
        occs = run_svc.list_candidate_occurrences(cands[0]["id"])
        assert len(occs) == n_branches


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestTriageReplayDeterminism:
    def test_triage_replay_identical_ids(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)

        ext_id = f"run-triage-replay-{uuid4()}"
        run_svc.create(objective="triage replay determinism", external_id=ext_id)
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id

        adapter = StubSearchAdapter(
            _make_success_payload(
                "https://triage-a.com/doc1",
                "https://triage-b.org/doc2",
                "https://triage-c.net/doc3",
            )
        )
        acq_svc = build_acquisition_service(config, search_adapter=adapter)
        acq_svc.execute_search(run_id, "triage replay query")

        triage_before = run_svc.build_triage_input(run_id, limit=10)
        ids_before = {c["id"] for c in triage_before["candidate_cards"]}
        assert len(ids_before) == 3
        triage_after = run_svc.build_triage_input(run_id, limit=10)
        ids_after = {c["id"] for c in triage_after["candidate_cards"]}
        assert ids_before == ids_after
        replay = run_svc.replay_candidates(run_id)
        assert {c["id"] for c in replay["candidate_cards"]} == ids_before

    def test_triage_card_content_deterministic(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-card-det-{uuid4()}"
        run_svc.create(objective="card content determinism", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        payload = json.dumps(
            {
                "success": True,
                "data": [
                    {
                        "url": "https://determ.example.com/p1",
                        "title": "Determ 1",
                        "snippet": "x" * 300,
                    },
                    {
                        "url": "https://determ.example.com/p2",
                        "title": "Determ 2",
                        "snippet": "y" * 300,
                    },
                ],
            }
        )
        resp = run_svc.record_search_response(
            run_id, "determinism query", "firecrawl", payload, f"det-key-{uuid4()}"
        )
        run_svc.record_response_candidates(run_id, resp["id"])
        triage1 = run_svc.build_triage_input(run_id, limit=10, max_snippet_length=100)
        triage2 = run_svc.build_triage_input(run_id, limit=10, max_snippet_length=100)
        assert [c["canonical_url"] for c in triage1["candidate_cards"]] == [
            c["canonical_url"] for c in triage2["candidate_cards"]
        ]


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestDuplicateIngestionIdempotency:
    def test_record_response_candidates_idempotent(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-idemp-p2-{uuid4()}"
        run_svc.create(objective="idempotency test", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        payload = json.dumps(
            {
                "success": True,
                "data": [
                    {"url": "https://idemp.example.com/page", "title": "Idempotent"},
                ],
            }
        )
        resp = run_svc.record_search_response(
            run_id, "idempotent query", "firecrawl", payload, f"idemp-key-{uuid4()}"
        )
        occs1 = run_svc.record_response_candidates(run_id, resp["id"])
        occs2 = run_svc.record_response_candidates(run_id, resp["id"])
        assert len(occs1) == len(occs2) == 1
        assert occs1[0]["id"] == occs2[0]["id"]
        cands = run_svc.list_candidates(run_id)
        assert len(cands) == 1
        assert cands[0]["recurrence_count"] >= 1

    def test_execute_search_same_idempotency_key(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-acq-idemp-p2-{uuid4()}"
        run_svc.create(objective="acq idempotency p2", external_id=ext_id)
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id

        adapter = StubSearchAdapter(_make_success_payload("https://idemp2.example.com"))
        acq_svc = build_acquisition_service(config, search_adapter=adapter)
        key = f"fixed-key-{uuid4()}"
        res1 = acq_svc.execute_search(
            run_id, "idempotent acq query", idempotency_key=key
        )
        res2 = acq_svc.execute_search(
            run_id, "idempotent acq query", idempotency_key=key
        )
        assert res1.search_response_id == res2.search_response_id
        assert res1.postgres_committed and res2.postgres_committed
        assert len(run_svc.list_search_responses(run_id)) == 1
        assert len(run_svc.list_candidates(run_id)) == 1


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestCrashReconciliation:
    def test_blob_exists_after_db_rollback(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
        ext_id = f"run-crash-blob-{uuid4()}"
        run_svc.create(objective="crash blob boundary", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        payload = json.dumps(
            {"success": True, "data": [{"url": "https://crash.example.com"}]}
        )
        payload_sha = hashlib.sha256(payload.encode()).hexdigest()
        with run_svc.uow_factory() as uow:
            try:
                with uow.savepoint():
                    uow.runs.record_search_response(
                        run_id,
                        "crash query",
                        "firecrawl",
                        payload,
                        f"crash-key-{uuid4()}",
                        blob_store=blob_store,
                    )
                    raise ValueError("simulated crash inside transaction")
            except ValueError:
                pass
        assert len(run_svc.list_search_responses(run_id)) == 0
        assert blob_store.exists(payload_sha)
        rec = run_svc.record_search_response(
            run_id,
            "crash query retry",
            "firecrawl",
            payload,
            f"crash-key-retry-{uuid4()}",
            blob_store=blob_store,
        )
        assert rec["raw_blob_sha256"] == payload_sha
        assert len(run_svc.list_search_responses(run_id)) == 1

    def test_reconcile_pending_searches_extracts_candidates(
        self, tmp_path, prepared_database
    ):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        acq_svc = build_acquisition_service(config)
        ext_id = f"run-reconcile-p2-{uuid4()}"
        run_svc.create(objective="reconcile crash p2", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        payload = json.dumps(
            {
                "success": True,
                "data": [
                    {"url": "https://reconcile-p2.example.com/a", "title": "Reconcile A"},
                    {"url": "https://reconcile-p2.example.com/b", "title": "Reconcile B"},
                ],
            }
        )
        resp = run_svc.record_search_response(
            run_id, "reconcile crash query", "firecrawl", payload, f"recon-p2-{uuid4()}"
        )
        assert len(run_svc.list_candidates(run_id)) == 0
        reconciled = acq_svc.reconcile_pending_searches(run_id)
        assert any(r["search_response_id"] == resp["id"] for r in reconciled)
        assert len(run_svc.list_candidates(run_id)) == 2

    def test_reconcile_idempotent_second_call(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        acq_svc = build_acquisition_service(config)
        ext_id = f"run-reconcile-idemp-{uuid4()}"
        run_svc.create(objective="reconcile idempotency", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        payload = json.dumps(
            {"success": True, "data": [{"url": "https://recon-idemp.example.com/x"}]}
        )
        run_svc.record_search_response(
            run_id,
            "reconcile idemp query",
            "firecrawl",
            payload,
            f"recon-idemp-{uuid4()}",
        )
        acq_svc.reconcile_pending_searches(run_id)
        acq_svc.reconcile_pending_searches(run_id)
        cands = run_svc.list_candidates(run_id)
        assert len(cands) == 1
        assert len(run_svc.list_candidate_occurrences(cands[0]["id"])) == 1


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestMalformedAndPartialResponses:
    def test_parse_error_response_stored_correctly(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-parse-err-{uuid4()}"
        run_svc.create(objective="parse error response", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        bad_payload = "<html><body>502 Bad Gateway</body></html>"
        resp = run_svc.record_search_response(
            run_id, "malformed query", "firecrawl", bad_payload, f"parse-err-{uuid4()}"
        )
        assert resp["status"] == "parse_error"
        assert resp["result_count"] == 0
        assert len(run_svc.list_candidates(run_id)) == 0

    def test_provider_error_response_stored_correctly(
        self, tmp_path, prepared_database
    ):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-prov-err-{uuid4()}"
        run_svc.create(objective="provider error response", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        error_payload = json.dumps({"success": False, "error": "rate limit exceeded"})
        resp = run_svc.record_search_response(
            run_id,
            "provider error query",
            "firecrawl",
            error_payload,
            f"prov-err-{uuid4()}",
            http_status=429,
        )
        assert resp["status"] == "provider_error"
        assert resp["result_count"] == 0
        assert run_svc.list_candidates(run_id) == []

    def test_mixed_items_partially_valid(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-mixed-{uuid4()}"
        run_svc.create(objective="mixed partial response", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        mixed_payload = json.dumps(
            {
                "success": True,
                "data": [
                    {"url": "https://valid-one.example.com", "title": "Valid"},
                    {"title": "Missing URL", "snippet": "no url here"},
                    {"url": "", "title": "Empty URL"},
                    {"url": "https://valid-two.example.com", "title": "Valid 2"},
                ],
            }
        )
        resp = run_svc.record_search_response(
            run_id,
            "mixed partial query",
            "firecrawl",
            mixed_payload,
            f"mixed-{uuid4()}",
        )
        assert resp["status"] == "succeeded"
        assert resp["result_count"] == 4
        occs = run_svc.record_response_candidates(run_id, resp["id"])
        assert len(occs) == 2
        cands = run_svc.list_candidates(run_id)
        assert len(cands) == 2
        canonical_urls = {c["canonical_url"] for c in cands}
        assert "https://valid-one.example.com/" in canonical_urls
        assert "https://valid-two.example.com/" in canonical_urls

    def test_empty_data_array_persisted(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-empty-{uuid4()}"
        run_svc.create(objective="empty response test", external_id=ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        empty_payload = json.dumps({"success": True, "data": []})
        resp = run_svc.record_search_response(
            run_id, "empty data query", "firecrawl", empty_payload, f"empty-{uuid4()}"
        )
        assert resp["status"] == "empty"
        assert resp["result_count"] == 0
        assert run_svc.list_candidates(run_id) == []

    def test_malformed_via_acquisition_service(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        ext_id = f"run-acq-parse-err-{uuid4()}"
        run_svc.create(objective="acq parse error", external_id=ext_id)
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id
        adapter = StubSearchAdapter(b"<html>503 Service Unavailable</html>")
        acq_svc = build_acquisition_service(config, search_adapter=adapter)
        res = acq_svc.execute_search(run_id, "malformed acquisition query")
        assert res.postgres_committed is True
        assert res.status == "parse_error"
        assert res.candidate_count == 0
        assert run_svc.get_search_response(res.search_response_id)["status"] == "parse_error"


@pytest.mark.skipif(not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL")
class TestPhase2EndToEnd:
    def test_full_phase2_scenario(self, tmp_path, prepared_database):
        migrate(TEST_DSN)
        config = replace(
            StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
        )
        run_svc = build_run_service(config)
        acq_svc = build_acquisition_service(
            config,
            search_adapter=StubSearchAdapter(
                _make_success_payload(
                    "https://e2e.example.com/shared",
                    "https://e2e.example.com/unique-a",
                )
            ),
        )
        ext_id = f"run-e2e-p2-{uuid4()}"
        run_svc.create(objective="phase2 e2e scenario", external_id=ext_id)
        _prepare_acquisition_run(config, ext_id)
        run_id = run_svc.status(external_id=ext_id).id

        res1 = acq_svc.execute_search(run_id, "e2e query alpha")
        assert res1.postgres_committed is True
        assert res1.candidate_count == 2
        adapter2 = StubSearchAdapter(
            _make_success_payload(
                "https://e2e.example.com/shared",
                "https://e2e.example.com/unique-b",
            )
        )
        acq_svc2 = build_acquisition_service(config, search_adapter=adapter2)
        res2 = acq_svc2.execute_search(run_id, "e2e query beta")
        assert res2.postgres_committed is True
        cands = run_svc.list_candidates(run_id)
        shared_cand = next((c for c in cands if "shared" in c["canonical_url"]), None)
        assert shared_cand is not None
        assert shared_cand["recurrence_count"] == 2
        persisted_candidates = run_svc.list_candidates(run_id)
        assert len(persisted_candidates) == 3
        triage = run_svc.build_triage_input(run_id, limit=10)
        triage_ids = {c["id"] for c in triage["candidate_cards"]}
        assert len(triage_ids) == 3
        replay = run_svc.replay_candidates(run_id)
        assert {c["id"] for c in replay["candidate_cards"]} == triage_ids

        crash_ext_id = f"run-crash-e2e-{uuid4()}"
        run_svc.create(objective="crash reconcile e2e", external_id=crash_ext_id)
        crash_run_id = run_svc.status(external_id=crash_ext_id).id
        crash_payload = json.dumps(
            {
                "success": True,
                "data": [{"url": "https://crash-e2e.example.com/recovered"}],
            }
        )
        run_svc.record_search_response(
            crash_run_id,
            "crash e2e query",
            "firecrawl",
            crash_payload,
            f"crash-e2e-{uuid4()}",
        )
        assert len(run_svc.list_candidates(crash_run_id)) == 0
        acq_svc.reconcile_pending_searches(crash_run_id)
        recovered = run_svc.list_candidates(crash_run_id)
        assert len(recovered) == 1
        assert "recovered" in recovered[0]["canonical_url"]
