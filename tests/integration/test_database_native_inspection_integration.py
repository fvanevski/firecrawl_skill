from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.inspection_contract import (
    InspectionError,
    InspectionNotFoundError,
    PageRequest,
    PassageBounds,
)
from firecrawl_skill.research_store.inspection_service import InspectionService
from firecrawl_skill.research_store.postgres import connect

DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not DSN, reason="disposable PostgreSQL is not configured"
)


def _config(tmp_path: Path) -> StoreConfig:
    values: dict[str, Any] = StoreConfig.from_env().__dict__ | {
        "database_url": DSN,
        "blob_root": tmp_path,
    }
    return StoreConfig(**values)


def _insert_fixture(tmp_path: Path) -> dict[str, Any]:
    ids: dict[str, Any] = {
        "run": uuid4(),
        "run2": uuid4(),
        "responses": [uuid4(), uuid4()],
        "candidate1": uuid4(),
        "candidate2": uuid4(),
        "candidate_other": uuid4(),
        "invocation1": uuid4(),
        "invocation2": uuid4(),
        "attempt1": uuid4(),
        "attempt2": uuid4(),
        "source": uuid4(),
        "snapshot": uuid4(),
        "document": uuid4(),
        "derivation": uuid4(),
        "chunks": [uuid4(), uuid4()],
    }
    # The storage-gate suite shares one disposable PostgreSQL lifecycle across
    # tests. Canonical source identity is globally unique, so this fixture must
    # not assume a common example URL is unoccupied by an earlier regression.
    fixture_host = f"inspection-{ids['run'].hex}.example.com"
    canonical = f"https://{fixture_host}/a"
    canonical2 = f"https://{fixture_host}/b"
    payload = json.dumps(
        {"success": True, "data": {"web": [{"url": canonical}]}}
    ).encode()
    blob = ContentAddressedBlobStore(tmp_path).put(
        __import__("io").BytesIO(payload), "application/json"
    )
    now = datetime.now(timezone.utc)
    with connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,'inspection integration','created','agent_led',%s),
                     (%s,'inspection other','created','agent_led',%s)""",
            (
                ids["run"],
                f"fr_{ids['run'].hex}",
                ids["run2"],
                f"fr_{ids['run2'].hex}",
            ),
        )
        for index, response_id in enumerate(ids["responses"]):
            cursor.execute(
                """INSERT INTO search_responses
                   (id,run_id,query_text,backend,status,http_status,parser_version,
                    raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                    result_count,idempotency_key,requested_at,responded_at,created_at)
                   VALUES(%s,%s,%s,'firecrawl','succeeded',200,'test-v1',
                          %s,%s,'application/json',%s,1,%s,%s,%s,%s)""",
                (
                    response_id,
                    ids["run"],
                    f"query-{index}",
                    blob.sha256,
                    blob.byte_length,
                    hashlib.sha256(payload).hexdigest(),
                    f"inspection:{response_id}",
                    now,
                    now,
                    now,
                ),
            )
        for candidate_id, run_id, url in (
            (ids["candidate1"], ids["run"], canonical),
            (ids["candidate2"], ids["run"], canonical2),
            (ids["candidate_other"], ids["run2"], "https://other.example/a"),
        ):
            cursor.execute(
                """INSERT INTO search_candidates
                   (id,run_id,canonical_url,canonical_url_sha256,original_url,
                    domain,backend)
                   VALUES(%s,%s,%s,%s,%s,%s,'firecrawl')""",
                (
                    candidate_id,
                    run_id,
                    url,
                    hashlib.sha256(url.encode()).hexdigest(),
                    url,
                    url.split("/")[2],
                ),
            )
        cursor.execute(
            """INSERT INTO candidate_occurrences
               (candidate_id,run_id,search_response_id,rank,query_text,original_url)
               VALUES(%s,%s,%s,1,'query-0',%s)""",
            (ids["candidate1"], ids["run"], ids["responses"][0], canonical),
        )
        for invocation_id, external_id, candidate_id, attempt_id, attempt_number in (
            (
                ids["invocation1"],
                f"fc_{uuid4().hex}",
                ids["candidate1"],
                ids["attempt1"],
                1,
            ),
            (
                ids["invocation2"],
                f"fc_{uuid4().hex}",
                ids["candidate2"],
                ids["attempt2"],
                1,
            ),
        ):
            request_input = {
                "schema_version": "direct-scrape-v1",
                "requests": [
                    {
                        "url": None,
                        "candidate_id": str(candidate_id),
                        "format": "markdown",
                        "summary": False,
                        "schema": None,
                        "mime_type": "text/markdown",
                        "options": {},
                    }
                ],
                "oversized": "x" * 20_000,
            }
            cursor.execute(
                """INSERT INTO research_invocations
                   (id,run_id,external_invocation_id,operation,status,
                    lifecycle_revision,idempotency_key,input,output,
                    started_at,completed_at)
                   VALUES(%s,%s,%s,'direct_scrape','complete',0,%s,%s,'{}',%s,%s)""",
                (
                    invocation_id,
                    ids["run"],
                    external_id,
                    f"integration:{invocation_id}",
                    json.dumps(request_input),
                    now,
                    now,
                ),
            )
            cursor.execute(
                """INSERT INTO extraction_attempts
                   (id,candidate_id,run_id,invocation_id,attempt_number,method,
                    method_version,requested_format,start_time,end_time,exit_status,
                    http_status,backend_status,failure_class,disposition,selected)
                   VALUES(%s,%s,%s,%s,%s,'firecrawl_main_content','test-v1',
                          'markdown',%s,%s,'succeeded',200,'complete','none',
                          'acceptable',true)""",
                (
                    attempt_id,
                    candidate_id,
                    ids["run"],
                    invocation_id,
                    attempt_number,
                    now,
                    now,
                ),
            )
        cursor.execute(
            """INSERT INTO sources(id,canonical_url,registered_domain,source_type)
               VALUES(%s,%s,'example.com','web')""",
            (ids["source"], canonical),
        )
        body = b"alpha database passage; beta.identifier and GammaToken"
        body_digest = hashlib.sha256(body).hexdigest()
        cursor.execute(
            """INSERT INTO asset_snapshots
               (id,source_id,extraction_attempt_id,requested_url,final_url,
                retrieved_at,mime_type,content_sha256,raw_blob_uri,
                raw_byte_length,crawl_options)
               VALUES(%s,%s,%s,%s,%s,%s,'text/markdown',%s,%s,%s,'{}')""",
            (
                ids["snapshot"],
                ids["source"],
                ids["attempt1"],
                canonical,
                canonical,
                now,
                body_digest,
                "blob://sha256/" + body_digest,
                len(body),
            ),
        )
        cursor.execute(
            """INSERT INTO documents
               (id,snapshot_id,extraction_attempt_id,title,parser_name,
                parser_version,normalization_version,document_sha256,metadata)
               VALUES(%s,%s,%s,'title','markdown','test-v1','test-v1',%s,'{}')""",
            (
                ids["document"],
                ids["snapshot"],
                ids["attempt1"],
                body_digest,
            ),
        )
        texts = (
            "alpha database passage; beta.identifier and GammaToken",
            "second database passage with foo-123 and BAR",
        )
        for ordinal, (chunk_id, text) in enumerate(
            zip(ids["chunks"], texts, strict=True)
        ):
            cursor.execute(
                """INSERT INTO chunks
                   (id,document_id,ordinal,text,token_count,content_sha256,
                    chunker_name,chunker_version,tokenizer_name,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,'hierarchical','test-v1',
                          'bpe_fake','{}')""",
                (
                    chunk_id,
                    ids["document"],
                    ordinal,
                    text,
                    len(text),
                    hashlib.sha256(text.encode()).hexdigest(),
                ),
            )
        cursor.execute(
            """INSERT INTO document_derivations
               (id,document_id,snapshot_id,status,parser_version,
                normalization_version,chunker_name,chunker_version,
                tokenizer_name,chunk_count,block_count,configuration_sha256)
               VALUES(%s,%s,%s,'active','test-v1','test-v1','hierarchical',
                      'test-v1','bpe_fake',2,0,%s)""",
            (
                ids["derivation"],
                ids["document"],
                ids["snapshot"],
                hashlib.sha256(b"integration-derivation").hexdigest(),
            ),
        )
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
               VALUES(%s,%s,'acquired',%s)""",
            (
                ids["run"],
                ids["snapshot"],
                json.dumps(
                    {
                        "candidate_id": str(ids["candidate2"]),
                        "invocation_id": str(ids["invocation2"]),
                    }
                ),
            ),
        )
        for invocation_id, candidate_id, attempt_id in (
            (ids["invocation1"], ids["candidate1"], ids["attempt1"]),
            (ids["invocation2"], ids["candidate2"], ids["attempt2"]),
        ):
            item = {
                "candidate_id": str(candidate_id),
                "invocation_id": str(invocation_id),
                "status": "succeeded",
                "extraction_attempt_id": str(attempt_id),
                "source_id": str(ids["source"]),
                "snapshot_id": str(ids["snapshot"]),
                "document_id": str(ids["document"]),
                "derivation_id": str(ids["derivation"]),
                "chunk_ids": [str(value) for value in ids["chunks"]],
            }
            cursor.execute(
                "UPDATE research_invocations SET output=%s WHERE id=%s",
                (
                    json.dumps({"schema_version": "direct-scrape-v1", "items": [item]}),
                    invocation_id,
                ),
            )
        connection.commit()
    ids["payload"] = payload
    return ids


def test_database_native_acceptance_paths(tmp_path):
    ids = _insert_fixture(tmp_path)
    calls: list[tuple[Any, Any, Any]] = []

    class Direct:
        def execute(self, run_id, requests, *, idempotency_key=None):
            calls.append((run_id, requests, idempotency_key))
            return SimpleNamespace(
                to_dict=lambda: {
                    "run_id": str(run_id),
                    "invocation_id": str(uuid4()),
                    "idempotency_key": idempotency_key,
                    "status": "complete",
                    "replayed": False,
                    "items": [
                        {
                            "candidate_id": str(requests[0].candidate_id),
                            "status": "succeeded",
                            "chunk_ids": [],
                        }
                    ],
                }
            )

        def retry_failed(self, run_id, requests, **kwargs):
            calls.append((run_id, requests, kwargs))
            return SimpleNamespace(
                to_dict=lambda: {
                    "run_id": str(run_id),
                    "invocation_id": str(uuid4()),
                    "idempotency_key": kwargs["idempotency_key"],
                    "status": "complete",
                    "replayed": False,
                    "items": [],
                }
            )

    inspector = InspectionService(
        _config(tmp_path), direct_scrape_factory=lambda: Direct()
    )

    first = inspector.list_search_responses(ids["run"], PageRequest(limit=1))
    second = inspector.list_search_responses(
        ids["run"], PageRequest(limit=1, cursor=first["next_cursor"])
    )
    assert second["items"][0]["id"] != first["items"][0]["id"]
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        inspector.list_search_responses(
            ids["run2"], PageRequest(limit=1, cursor=first["next_cursor"])
        )

    replay = inspector.replay_search(ids["responses"][0])
    assert replay["payload_integrity"]["verified"] is True
    assert replay["candidates"][0]["candidate_id"] == str(ids["candidate1"])
    with pytest.raises(InspectionNotFoundError):
        inspector.replay_search(uuid4())

    scrape = inspector.scrape_candidates(
        [ids["candidate1"]], idempotency_key="integration-scrape"
    )
    assert scrape["status"] == "complete"
    assert calls[-1][0] == ids["run"]
    with pytest.raises(InspectionError, match="one research run"):
        inspector.scrape_candidates([ids["candidate1"], ids["candidate_other"]])

    retry = inspector.retry_candidates(
        ids["invocation2"], idempotency_key="integration-retry"
    )
    assert retry["kind"] == "candidate_retry"
    assert calls[-1][2]["prior_invocation_id"] == ids["invocation2"]

    attempts = inspector.list_extraction_attempts(candidate_id=ids["candidate2"])
    item = attempts["items"][0]
    assert item["snapshot_id"] == str(ids["snapshot"])
    assert item["document_id"] == str(ids["document"])
    assert item["derivation_id"] == str(ids["derivation"])
    assert item["chunk_ids"]["items"] == [str(value) for value in ids["chunks"]]
    with pytest.raises(InspectionNotFoundError, match="candidate not found"):
        inspector.list_extraction_attempts(candidate_id=uuid4())

    passages = []
    cursor = None
    while True:
        page = inspector.passages(
            ids["attempt2"],
            PassageBounds(limit=1, max_chars=7, max_tokens=100, cursor=cursor),
        )
        passages.extend(value["text"] for value in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = (
        "alpha database passage; beta.identifier and GammaToken"
        "second database passage with foo-123 and BAR"
    )
    assert "".join(passages) == expected

    lexical = inspector.lexical_search(
        "database", run=ids["run"], bounds=PassageBounds(limit=2)
    )
    assert lexical["item_count"] == 2
    literal = inspector.pattern_search(
        "beta.identifier", mode="literal", run=ids["run"]
    )
    assert literal["item_count"] == 1
    regex = inspector.pattern_search(
        "foo-[0-9]+|gammatoken", mode="regex", run=ids["run"]
    )
    assert regex["item_count"] == 2

    invocations = inspector.list_invocations(ids["run"], PageRequest(limit=10))
    assert any(row["input"]["truncated"] for row in invocations["items"])
    assert (
        invocations["output_bounds"]["serialized_chars"]
        <= invocations["output_bounds"]["max_serialized_chars"]
    )
