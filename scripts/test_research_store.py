from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from io import BytesIO
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.compat import import_scratch
from research_store.domain import IngestResult
from research_store.parsing import deterministic_chunks, structural_blocks
from research_store.retrieval import (
    pack_context,
    reciprocal_rank_fusion,
    validate_relation,
)
from research_store.url import canonicalize_url


def test_url_canonicalization():
    assert (
        canonicalize_url("HTTPS://Example.COM:443/a/?utm_source=x&b=2&a=1#frag")
        == "https://example.com/a?a=1&b=2"
    )
    with pytest.raises(ValueError):
        canonicalize_url("file:///etc/passwd")


def test_atomic_content_addressed_blob_write_and_dedup(tmp_path):
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.put(BytesIO(b"immutable"), "text/plain")
    second = store.put(BytesIO(b"immutable"), "text/plain")
    assert first.sha256 == second.sha256
    assert store.path_for(first.sha256).relative_to(store.root).parts == (
        first.sha256[:2],
        first.sha256[2:4],
        first.sha256,
    )
    assert store.verify(first.sha256)
    assert len(list((tmp_path / "blobs").rglob(first.sha256))) == 1


def test_structural_parser_and_deterministic_chunks_preserve_provenance():
    source = "# Title\n\nParagraph one.\n\n- item\n\n> quote\n\n```py\nprint(1)\n```\n"
    blocks = structural_blocks(source)
    assert [b.block_type for b in blocks] == [
        "heading",
        "paragraph",
        "list_item",
        "quotation",
        "code",
    ]
    assert all(
        block.char_start is not None
        and source[block.char_start : block.char_end].strip()
        for block in blocks
    )
    first = deterministic_chunks(blocks, max_chars=30)
    second = deterministic_chunks(blocks, max_chars=30)
    assert first == second
    assert all(chunk.first_block_ordinal <= chunk.last_block_ordinal for chunk in first)


def test_rank_fusion_and_context_budget():
    fused = reciprocal_rank_fusion(
        [
            [
                {"candidate_id": "a", "retriever": "lexical"},
                {"candidate_id": "b", "retriever": "lexical"},
            ],
            [
                {"candidate_id": "b", "retriever": "semantic"},
                {"candidate_id": "a", "retriever": "semantic"},
            ],
        ]
    )
    assert {item["candidate_id"] for item in fused} == {"a", "b"}
    assert all(len(item["match_reasons"]) == 2 for item in fused)
    merged = reciprocal_rank_fusion(
        [
            [{"candidate_id": "a", "lexical_score": 0.5}],
            [{"candidate_id": "a", "semantic_score": 0.8}],
        ]
    )[0]
    assert merged["lexical_score"] == 0.5
    assert merged["semantic_score"] == 0.8
    assert pack_context(
        [{"text": "a", "token_count": 3}, {"text": "b", "token_count": 4}], 4, 5
    ) == [{"text": "a", "token_count": 3}]


def test_relation_class_requires_model_provenance():
    validate_relation({"relation_class": "observed", "object_literal": "x"})
    validate_relation(
        {
            "relation_class": "model_inferred",
            "object_literal": "x",
            "extraction_model": "local/model",
        }
    )
    with pytest.raises(ValueError):
        validate_relation({"relation_class": "model_inferred", "object_literal": "x"})


class FakeService:
    def __init__(self):
        self.calls = []

    def ingest(self, request):
        self.calls.append(request)
        value = uuid4()
        return IngestResult(
            value, value, value, (value,), "a" * 64, len(self.calls) > 1
        )


def test_scratch_import_is_idempotent_and_reports_failures(tmp_path):
    scratch = tmp_path / "fc_test" / "search"
    scratch.mkdir(parents=True)
    asset = scratch / "result_000.md"
    asset.write_text("# Imported\n\nExact source.")
    (scratch / "_meta.json").write_text(
        json.dumps(
            {
                "invocation_id": "fc_test",
                "operation": "search",
                "results": [
                    {
                        "index": 0,
                        "url": "https://example.com/a",
                        "title": "Imported",
                        "scratch_file": str(asset),
                        "status": "ok",
                    }
                ],
            }
        )
    )
    service = FakeService()
    first = import_scratch(tmp_path, service)
    second = import_scratch(tmp_path, service)
    assert first["imported"] == 1 and second["reused"] == 1
    assert service.calls[0].metadata["migration"]["original_path"] == str(asset)
    dry = import_scratch(tmp_path, None, dry_run=True)
    assert dry["items"][0]["status"] == "would_import"
