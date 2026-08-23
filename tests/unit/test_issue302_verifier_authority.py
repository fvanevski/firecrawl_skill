"""Issue #302 regressions for PostgreSQL-authoritative run blob verification."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.run_service import ResearchRunService

RUN_ID = UUID(int=302)


class _Runs:
    def __init__(self, invocations: list[dict[str, Any]] | None = None) -> None:
        self.invocations = invocations or []

    def list_invocations(self, run_id: UUID) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return list(self.invocations)


class _SearchResponses:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def list_search_responses(self, run_id: UUID) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return list(self.rows)


class _ExtractionAttempts:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def count_for_run(self, run_id: UUID) -> int:
        assert run_id == RUN_ID
        return len(self.rows)

    def list_attempts_for_run(
        self,
        run_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return self.rows[offset : offset + limit]


class _Snapshots:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def list_run_blob_references(
        self,
        run_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return self.rows[offset : offset + limit]


class _ForbiddenChunks:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"verifier must not consult chunk hashes: {name}")


class _UnitOfWork:
    def __init__(
        self,
        *,
        searches: list[dict[str, Any]] | None = None,
        attempts: list[dict[str, Any]] | None = None,
        snapshots: list[dict[str, Any]] | None = None,
        invocations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.runs = _Runs(invocations)
        self.search_responses = _SearchResponses(searches or [])
        self.extraction_attempts = _ExtractionAttempts(attempts or [])
        self.snapshots = _Snapshots(snapshots or [])
        self.chunks = _ForbiddenChunks()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _service(
    store: Any,
    *,
    searches: list[dict[str, Any]] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    invocations: list[dict[str, Any]] | None = None,
) -> ResearchRunService:
    return ResearchRunService(
        lambda: _UnitOfWork(
            searches=searches,
            attempts=attempts,
            snapshots=snapshots,
            invocations=invocations,
        ),
        blob_store=store,
    )


def test_normal_markdown_refs_are_conclusive_and_count_logical_duplicates(
    tmp_path: Path,
) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    search_blob = store.put(BytesIO(b'{"data":[{"url":"https://example.test"}]}'))
    markdown_blob = store.put(BytesIO(b"# retained markdown\n"))

    report = _service(
        store,
        searches=[{"id": uuid4(), "raw_blob_sha256": search_blob.sha256}],
        attempts=[
            {
                "id": uuid4(),
                "raw_blob_sha256": search_blob.sha256,
                "normalized_blob_sha256": markdown_blob.sha256,
            }
        ],
        snapshots=[
            {
                "id": uuid4(),
                "content_sha256": markdown_blob.sha256,
                "raw_blob_uri": f"sha256://{markdown_blob.sha256}",
            }
        ],
    ).verify(RUN_ID)

    assert report["status"] == "passed"
    assert report["total"] == 4
    assert report["unique_blobs"] == 2
    assert report["available"] == 4
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0
    assert {item["source"] for item in report["artifacts"]} == {
        "search_response",
        "extraction_attempt",
        "asset_snapshot",
    }


def test_missing_and_corrupt_postgres_refs_fail_closed(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    missing = hashlib.sha256(b"missing").hexdigest()
    corrupt = hashlib.sha256(b"expected").hexdigest()
    corrupt_path = store.path_for(corrupt)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"tampered")

    report = _service(
        store,
        searches=[{"id": uuid4(), "raw_blob_sha256": missing}],
        attempts=[
            {
                "id": uuid4(),
                "raw_blob_sha256": corrupt,
                "normalized_blob_sha256": None,
            }
        ],
    ).verify(RUN_ID)

    assert report["status"] == "failed"
    assert report["total"] == 2
    assert report["unique_blobs"] == 2
    assert report["available"] == 0
    assert report["missing"] == 1
    assert report["hash_mismatch"] == 1


def test_chunk_content_hashes_are_not_blob_evidence(tmp_path: Path) -> None:
    report = _service(ContentAddressedBlobStore(tmp_path)).verify(RUN_ID)

    assert report["status"] == "inconclusive"
    assert report["total"] == 0
    assert report["unique_blobs"] == 0


def test_legacy_verify_only_blob_store_contract_is_preserved() -> None:
    good_digest = "a" * 64
    bad_digest = "b" * 64

    class _VerifyOnlyBlobStore:
        def verify(self, digest: str) -> bool:
            return digest == good_digest

    report = _service(
        _VerifyOnlyBlobStore(),
        invocations=[
            {
                "id": "legacy-invocation",
                "output": {
                    "results": [
                        {
                            "snapshot": {
                                "path": f"/blob/{good_digest}",
                                "sha256": good_digest,
                            }
                        },
                        {
                            "artifacts": [
                                {
                                    "path": f"/blob/{bad_digest}",
                                    "sha256": bad_digest,
                                }
                            ]
                        },
                    ]
                },
            }
        ],
    ).verify(RUN_ID)

    assert report["status"] == "failed"
    assert report["total"] == 2
    assert report["available"] == 1
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 1
