from __future__ import annotations

import ast
from pathlib import Path

import research_store
from research_store.corpus_service import CorpusService, ParsedContent, PreparedIngest
from research_store.service import (
    CorpusService as CompatibilityCorpusService,
    ParsedContent as CompatibilityParsedContent,
    PreparedIngest as CompatibilityPreparedIngest,
)


_STORE = Path(__file__).resolve().parent / "research_store"


def _class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def test_corpus_types_live_in_canonical_slice_with_bounded_service_facade() -> None:
    assert CorpusService.__module__.endswith(".corpus_service")
    assert CompatibilityCorpusService is CorpusService
    assert CompatibilityParsedContent is ParsedContent
    assert CompatibilityPreparedIngest is PreparedIngest
    assert research_store.CorpusService is CorpusService

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
    prepared = PreparedIngest(
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
    assert CorpusService.ingest_batch.__module__.endswith(".ingestion_batch_semantics")
    assert CorpusService.finalize_ingestion_batch.__module__.endswith(
        ".ingestion_batch_semantics"
    )
