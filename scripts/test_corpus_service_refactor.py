from __future__ import annotations

import ast
from pathlib import Path

from research_store import corpus_service, service

_STORE = Path(__file__).resolve().parent / "research_store"


def _class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def test_corpus_types_live_in_canonical_slice_with_bounded_service_facade() -> None:
    corpus_type = corpus_service.CorpusService
    assert corpus_type.__module__.endswith(".corpus_service")
    assert service.CorpusService is corpus_type
    assert service.ParsedContent is corpus_service.ParsedContent
    assert service.PreparedIngest is corpus_service.PreparedIngest
    assert service.IngestRequest is corpus_service.IngestRequest
    assert service.IngestResult is corpus_service.IngestResult
    assert __import__("research_store").CorpusService is corpus_type

    assert {"CorpusService", "ParsedContent", "PreparedIngest"}.isdisjoint(
        _class_names(_STORE / "service.py")
    )
    assert {"CorpusService", "ParsedContent", "PreparedIngest"}.issubset(
        _class_names(_STORE / "corpus_service.py")
    )


def test_prepared_ingest_preserves_parser_and_chunker_provenance_contract() -> None:
    request = object()
    blob = object()
    blocks = (object(),)
    chunks = (object(),)
    prepared = corpus_service.PreparedIngest(
        request=request,
        canonical_url="https://example.test/",
        blob=blob,
        normalized_text="body",
        blocks=blocks,
        chunks=chunks,
        parser_name="pkg.Parser",
        parser_version="parser-contract-v3",
        parser_implementation_version="impl-7",
        chunker_version="chunker-v4",
        normalization_version="normalization-v2",
        chunker_name="hierarchical",
    )

    assert prepared.persist_args() == (
        request,
        "https://example.test/",
        blob,
        "body",
        blocks,
        chunks,
        "parser-contract-v3",
        "chunker-v4",
        "normalization-v2",
        "hierarchical",
        "pkg.Parser@impl-7",
    )


def test_issue_217_batch_contract_patches_the_canonical_corpus_class() -> None:
    assert corpus_service.CorpusService.ingest_batch.__module__.endswith(
        ".ingestion_batch_semantics"
    )
    assert corpus_service.CorpusService.finalize_ingestion_batch.__module__.endswith(
        ".ingestion_batch_semantics"
    )
