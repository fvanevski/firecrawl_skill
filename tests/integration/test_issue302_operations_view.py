from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.inspection_contract import PageRequest
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


def test_operations_is_union_without_redefining_invocations_or_attempts(tmp_path: Path):
    run_id = uuid4()
    external_id = f"fr_{run_id.hex}"
    candidate_ids = [uuid4() for _ in range(6)]
    invocation_ids = [uuid4() for _ in range(3)]
    attempt_ids = [uuid4() for _ in range(6)]
    now = datetime.now(timezone.utc)

    with connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,'issue 302 operations','created','autonomous_local',%s)""",
            (run_id, external_id),
        )
        for index, candidate_id in enumerate(candidate_ids):
            url = f"https://operations-{run_id.hex}.example/{index}"
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
                    f"operations-{run_id.hex}.example",
                ),
            )
        for index, invocation_id in enumerate(invocation_ids):
            stamp = now - timedelta(seconds=index)
            cursor.execute(
                """INSERT INTO research_invocations
                   (id,run_id,operation,status,lifecycle_revision,idempotency_key,
                    input,output,started_at,completed_at,created_at)
                   VALUES(%s,%s,'fsearch','complete',0,%s,%s,'{}',%s,%s,%s)""",
                (
                    invocation_id,
                    run_id,
                    f"issue302:search:{index}:{run_id}",
                    json.dumps({"query_text": f"query {index}"}),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        for index, (attempt_id, candidate_id) in enumerate(
            zip(attempt_ids, candidate_ids, strict=True)
        ):
            stamp = now - timedelta(seconds=10 + index)
            failed = index == 5
            cursor.execute(
                """INSERT INTO extraction_attempts
                   (id,candidate_id,run_id,attempt_number,method,method_version,
                    requested_format,start_time,end_time,exit_status,http_status,
                    backend_status,failure_class,disposition,error_message,selected,
                    created_at)
                   VALUES(%s,%s,%s,%s,'firecrawl_main_content','test-v1','markdown',
                          %s,%s,%s,%s,%s,%s,'acceptable',%s,%s,%s)""",
                (
                    attempt_id,
                    candidate_id,
                    run_id,
                    index + 1,
                    stamp,
                    stamp,
                    "cancelled" if failed else "succeeded",
                    None if failed else 200,
                    "preflight:first_byte_timeout" if failed else "complete",
                    "timeout" if failed else "none",
                    "first byte timeout" if failed else None,
                    not failed,
                    stamp,
                ),
            )
        connection.commit()

    inspector = InspectionService(_config(tmp_path))
    invocations = inspector.list_invocations(run_id, PageRequest(limit=20))
    attempts = inspector.list_extraction_attempts(
        run=run_id, page=PageRequest(limit=20)
    )
    operations = inspector.list_operations(run_id, PageRequest(limit=20))

    assert invocations["item_count"] == 3
    assert attempts["item_count"] == 6
    assert operations["item_count"] == 9
    assert {item["record_kind"] for item in operations["items"]} == {
        "invocation",
        "extraction_attempt",
    }
    timeout = [
        item
        for item in operations["items"]
        if item["record_kind"] == "extraction_attempt"
        and item["failure_class"] == "timeout"
    ]
    assert len(timeout) == 1
    assert timeout[0]["related_attempt_id"] == str(attempt_ids[5])
    assert timeout[0]["operation_kind"] == "scrape"

    first = inspector.list_operations(run_id, PageRequest(limit=4))
    second = inspector.list_operations(
        run_id, PageRequest(limit=4, cursor=first["next_cursor"])
    )
    third = inspector.list_operations(
        run_id, PageRequest(limit=4, cursor=second["next_cursor"])
    )
    paged_ids = [
        item["id"] for page in (first, second, third) for item in page["items"]
    ]
    assert len(paged_ids) == 9
    assert len(set(paged_ids)) == 9


def test_operations_uses_explicit_taxonomy_and_operation_appropriate_targets(
    tmp_path: Path,
):
    run_id = uuid4()
    external_id = f"fr_{run_id.hex}"
    candidate_id = uuid4()
    invocation_ids = {
        name: uuid4()
        for name in (
            "fresearch_planning",
            "search_provider",
            "direct_scrape",
            "research",
            "searchable",
            "scrape_metadata",
        )
    }
    attempt_id = uuid4()
    now = datetime.now(timezone.utc)
    scrape_url = f"https://taxonomy-{run_id.hex}.example/article"

    invocation_inputs = {
        "fresearch_planning": {"objective": "research the current state"},
        "search_provider": {"query_text": "authoritative provider query"},
        "direct_scrape": {
            "requests": [
                {
                    "url": scrape_url,
                    "candidate_id": str(candidate_id),
                }
            ]
        },
        "research": {"query_text": "not a provider query"},
        "searchable": {"query_text": "not a provider query"},
        "scrape_metadata": {"requests": [{"url": scrape_url}]},
    }

    with connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,'issue 368 taxonomy','created','autonomous_local',%s)""",
            (run_id, external_id),
        )
        cursor.execute(
            """INSERT INTO search_candidates
               (id,run_id,canonical_url,canonical_url_sha256,original_url,
                domain,backend)
               VALUES(%s,%s,%s,%s,%s,%s,'firecrawl')""",
            (
                candidate_id,
                run_id,
                scrape_url,
                hashlib.sha256(scrape_url.encode()).hexdigest(),
                scrape_url,
                f"taxonomy-{run_id.hex}.example",
            ),
        )
        for index, (operation, invocation_id) in enumerate(invocation_ids.items()):
            stamp = now - timedelta(seconds=index)
            cursor.execute(
                """INSERT INTO research_invocations
                   (id,run_id,operation,status,lifecycle_revision,idempotency_key,
                    input,output,started_at,completed_at,created_at)
                   VALUES(%s,%s,%s,'complete',0,%s,%s,'{}',%s,%s,%s)""",
                (
                    invocation_id,
                    run_id,
                    operation,
                    f"issue368:{operation}:{run_id}",
                    json.dumps(invocation_inputs[operation]),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        attempt_stamp = now - timedelta(seconds=20)
        cursor.execute(
            """INSERT INTO extraction_attempts
               (id,candidate_id,run_id,invocation_id,attempt_number,method,
                method_version,requested_format,start_time,end_time,exit_status,
                http_status,backend_status,failure_class,disposition,selected,
                created_at)
               VALUES(%s,%s,%s,%s,1,'firecrawl_main_content','test-v1','markdown',
                      %s,%s,'succeeded',200,'complete','none','acceptable',TRUE,%s)""",
            (
                attempt_id,
                candidate_id,
                run_id,
                invocation_ids["direct_scrape"],
                attempt_stamp,
                attempt_stamp,
                attempt_stamp,
            ),
        )
        connection.commit()

    operations = InspectionService(_config(tmp_path)).list_operations(
        run_id, PageRequest(limit=20)
    )
    by_id = {item["id"]: item for item in operations["items"]}

    planning = by_id[str(invocation_ids["fresearch_planning"])]
    assert planning["operation_kind"] == "fresearch_planning"
    assert planning["target"] is None

    provider_search = by_id[str(invocation_ids["search_provider"])]
    assert provider_search["operation_kind"] == "search"
    assert provider_search["target"] == "authoritative provider query"

    provider_scrape = by_id[str(invocation_ids["direct_scrape"])]
    assert provider_scrape["operation_kind"] == "scrape"
    assert provider_scrape["target"] == scrape_url

    for operation in ("research", "searchable", "scrape_metadata"):
        item = by_id[str(invocation_ids[operation])]
        assert item["operation_kind"] == operation
        assert item["target"] is None

    extraction = by_id[str(attempt_id)]
    assert extraction["record_kind"] == "extraction_attempt"
    assert extraction["operation_kind"] == "scrape"
    assert extraction["target"] == scrape_url
    assert extraction["related_attempt_id"] == str(attempt_id)
    assert extraction["related_invocation_id"] == str(invocation_ids["direct_scrape"])
    assert extraction["id"] != extraction["related_invocation_id"]
