from __future__ import annotations

import ast
from pathlib import Path

from research_store import corpus_service, retrieval_service, service
from research_store.domain import IngestRequest

_STORE = Path(__file__).resolve().parent / "research_store"
_RETRIEVAL_METHODS = {
    "search_assets",
    "get_retrieval_trace",
    "inspect_asset",
    "fetch_passages",
    "select_run_passages",
    "build_evidence_packet",
    "expand_relationships",
}


def _class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _class_method_names(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"class {class_name} not found in {path}")


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def test_retrieval_behavior_is_extracted_from_canonical_corpus_implementation() -> None:
    corpus_type = corpus_service.CorpusService
    retrieval_path = _STORE / "retrieval" / "service.py"
    assert issubclass(corpus_type, retrieval_service.RetrievalService)
    assert _RETRIEVAL_METHODS.isdisjoint(
        _class_method_names(_STORE / "corpus_service.py", "CorpusService")
    )
    assert _RETRIEVAL_METHODS.issubset(
        _class_method_names(retrieval_path, "RetrievalService")
    )
    assert corpus_type.search_assets.__module__.endswith(".retrieval.service")
    assert corpus_type.build_evidence_packet.__module__.endswith(".retrieval.service")

    corpus_helpers = _function_names(_STORE / "corpus_service.py")
    retrieval_helpers = _function_names(retrieval_path)
    assert {"_semantic_candidate", "_qdrant_filter"}.isdisjoint(corpus_helpers)
    assert {"_semantic_candidate", "_qdrant_filter"}.issubset(retrieval_helpers)


def test_internal_corpus_builders_import_the_canonical_slice() -> None:
    composition_source = (_STORE / "composition.py").read_text(encoding="utf-8")
    container_source = (_STORE / "container.py").read_text(encoding="utf-8")
    direct_scrape_facade = (_STORE / "acquisition" / "direct_scrape.py").read_text(
        encoding="utf-8"
    )
    direct_scrape_application = (
        _STORE / "acquisition" / "direct_scrape_application.py"
    ).read_text(encoding="utf-8")

    assert "from .service import CorpusService" not in composition_source
    assert "from .corpus_service import CorpusService" in composition_source

    assert "from .corpus_service import CorpusService" not in container_source
    assert "from .composition import (" in container_source

    assert "CorpusService" not in direct_scrape_application
    assert "from .. import composition as _composition" not in direct_scrape_application
    assert "from .. import composition as _composition" in direct_scrape_facade
    assert "_composition.build_direct_scrape_service(" in direct_scrape_facade


def test_prepared_ingest_preserves_parser_and_chunker_provenance_contract() -> None:
    request = IngestRequest(
        requested_url="https://example.test/",
        final_url="https://example.test/",
        content=b"body",
        normalized_content=b"body",
        mime_type="text/plain",
    )
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
