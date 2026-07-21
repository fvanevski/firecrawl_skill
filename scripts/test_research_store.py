from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.cli import parser as research_store_parser
from research_store.compat import import_scratch
from research_store.domain import IngestResult
from research_store.parsing import deterministic_chunks, structural_blocks
from research_store.postgres import require_disposable_database_reset
from research_store.retrieval import (
    pack_context,
    reciprocal_rank_fusion,
    validate_relation,
)
from research_store.run_service import (
    PERMITTED_TRANSITIONS,
    RUN_STATES,
    TERMINAL_STATES,
    is_transition_permitted,
)
from research_store.service import CorpusService
from research_store.url import canonicalize_url
import persist_results


def test_destructive_integration_database_guard():
    with pytest.raises(RuntimeError, match="standalone 'test' segment"):
        require_disposable_database_reset(
            "postgresql://research_app@localhost/research_assets", "research_assets"
        )
    with pytest.raises(RuntimeError, match="must equal the exact database name"):
        require_disposable_database_reset(
            "postgresql://research_app@localhost/research_assets_test_codex", "wrong"
        )
    assert (
        require_disposable_database_reset(
            "postgresql://research_app@localhost/research_assets_test_codex",
            "research_assets_test_codex",
        )
        == "research_assets_test_codex"
    )


def test_run_finish_parser_rejects_nonterminal_status():
    with pytest.raises(SystemExit):
        research_store_parser().parse_args(
            [
                "run-finish",
                "fr_test",
                "--outcome",
                "satisfied",
                "--status",
                "running",
            ]
        )


def test_research_run_transition_matrix_is_exact():
    expected = {
        ("created", "planning"),
        ("planning", "corpus_review"),
        ("planning", "failed"),
        ("corpus_review", "acquiring"),
        ("corpus_review", "retrieving"),
        ("corpus_review", "failed"),
        ("acquiring", "coverage_review"),
        ("acquiring", "extracting"),
        ("acquiring", "failed"),
        ("extracting", "indexing"),
        ("extracting", "coverage_review"),
        ("extracting", "failed"),
        ("indexing", "coverage_review"),
        ("indexing", "partial"),
        ("indexing", "failed"),
        ("coverage_review", "acquiring"),
        ("coverage_review", "extracting"),
        ("coverage_review", "retrieving"),
        ("coverage_review", "synthesizing"),
        ("coverage_review", "partial"),
        ("coverage_review", "failed"),
        ("retrieving", "coverage_review"),
        ("retrieving", "synthesizing"),
        ("retrieving", "failed"),
        ("synthesizing", "validating"),
        ("synthesizing", "failed"),
        ("validating", "completed"),
        ("validating", "partial"),
        ("validating", "failed"),
    }
    actual = {
        (prior, following)
        for prior in RUN_STATES
        for following in RUN_STATES
        if is_transition_permitted(prior, following)
    }
    assert actual == expected
    assert set(PERMITTED_TRANSITIONS) == set(RUN_STATES)
    assert all(not PERMITTED_TRANSITIONS[state] for state in TERMINAL_STATES)


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


def test_wrapper_persistence_writes_failure_export_when_store_is_unavailable(
    tmp_path, monkeypatch
):
    asset = tmp_path / "asset.md"
    asset.write_text("# Retained success\n")
    meta = tmp_path / "_meta.json"
    meta.write_text(
        json.dumps(
            {
                "invocation_id": "fc_" + "a" * 32,
                "operation": "scrape",
                "results": [
                    {
                        "index": 0,
                        "url": "https://fixture.invalid/fail-closed",
                        "status": "ok",
                        "scratch_file": str(asset),
                    }
                ],
            }
        )
    )

    class UnavailableService:
        def persist_manifest_batch(self, *_args, **_kwargs):
            raise OSError("database unavailable")

    output = tmp_path / "_corpus.json"
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    monkeypatch.setattr(persist_results, "build_service", lambda: UnavailableService())
    assert persist_results.main([str(meta), "--output", str(output)]) == 1
    exported = json.loads(output.read_text())
    assert exported["status"] == "failed"
    assert exported["assets"][0]["requested_url"].endswith("fail-closed")


def test_wrapper_empty_raw_path_falls_back_to_normalized_scratch(tmp_path, monkeypatch):
    asset = tmp_path / "smart-result.md"
    asset.write_text("# Consolidated parent\n\nRetained exactly once.\n")
    meta = tmp_path / "_meta.json"
    meta.write_text(
        json.dumps(
            {
                "invocation_id": "fc_" + "b" * 32,
                "operation": "smart_search",
                "results": [
                    {
                        "index": 0,
                        "url": "https://fixture.invalid/smart-parent",
                        "status": "ok",
                        "scratch_file": str(asset),
                        "raw_scratch_file": "",
                    }
                ],
            }
        )
    )

    class CapturingService:
        def persist_manifest_batch(self, metadata, assets, **_options):
            request = assets[0]["request"]
            assert request.content == request.normalized_content == asset.read_bytes()
            return {
                "invocation_id": metadata["invocation_id"],
                "operation": metadata["operation"],
                "status": "complete",
                "assets": [{"status": "complete"}],
            }

    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    monkeypatch.setattr(persist_results, "build_service", CapturingService)
    assert (
        persist_results.main([str(meta), "--output", str(tmp_path / "_corpus.json")])
        == 0
    )


def test_search_skips_semantic_embedding_when_active_alias_has_other_model():
    candidate_id = uuid4()

    class Repository:
        documents = None

        def __init__(self):
            self.documents = self

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "lexical fallback"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class WrongAliasIndex:
        def list_aliases(self):
            return {"active": "research_chunks_other_model"}

        def search(self, *_args):
            raise AssertionError("semantic search must not use a mismatched alias")

    def forbidden_embedder(_query):
        raise AssertionError("query must not be embedded for a mismatched alias")

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="research_chunks_configured_model",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
    )
    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=WrongAliasIndex(),
        embedder=forbidden_embedder,
    )

    results = service.search_assets("fallback", candidate_limit=5)
    assert [result["candidate_id"] for result in results] == [str(candidate_id)]
    assert results[0]["excerpt"] == "lexical fallback"
