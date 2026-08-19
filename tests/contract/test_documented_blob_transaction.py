"""Exercise the documented Target A blob and metadata crash window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_blob_install_precedes_metadata_and_commit_failure_returns_no_result() -> None:
    from research_store.domain import IngestRequest
    from research_store.service import CorpusService

    events: list[str] = []

    class BlobStore:
        def put(self, *_args, **_kwargs):
            events.append("blob-installed")
            return SimpleNamespace(
                sha256="0" * 64,
                byte_length=7,
                mime_type="text/plain",
            )

    class Snapshots:
        def persist_ingest(self, *_args, **_kwargs):
            events.append("metadata-write")
            raise RuntimeError("injected metadata failure")

    class UnitOfWork:
        snapshots = Snapshots()

        def __enter__(self):
            events.append("transaction-opened")
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            events.append("transaction-rolled-back")
            return False

    notifications: list[object] = []
    config = SimpleNamespace(
        chunker_max_tokens=256,
        tokenizer_name="cl100k_base",
        chunker_version="test-chunker",
        chunker_name="hierarchical",
        parser_version="test-parser",
        normalization_version="test-normalizer",
    )
    service = CorpusService(
        config,
        UnitOfWork,
        BlobStore(),
        queue=SimpleNamespace(notify=notifications.append),
    )
    request = IngestRequest(
        requested_url="https://example.com",
        content=b"payload",
        mime_type="text/plain",
    )

    with pytest.raises(RuntimeError, match="injected metadata failure"):
        service.ingest(request)

    assert events == [
        "blob-installed",
        "transaction-opened",
        "metadata-write",
        "transaction-rolled-back",
    ]
    assert notifications == []
