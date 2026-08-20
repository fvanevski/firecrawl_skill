from __future__ import annotations

import ast
from pathlib import Path

from firecrawl_skill.research_store import corpus_service
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.retrieval.service import RetrievalService

_STORE = (
    Path(__file__).resolve().parents[2] / "src" / "firecrawl_skill" / "research_store"
)
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


def _imports_name(source: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == name or alias.name.endswith(f".{name}")
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == name or module.endswith(f".{name}"):
                return True
            if any(alias.name == name for alias in node.names):
                return True
    return False


def test_corpus_types_live_only_in_canonical_slice() -> None:
    corpus_type = corpus_service.CorpusService
    assert corpus_type.__module__.endswith(".corpus_service")
    assert {"CorpusService", "ParsedContent", "PreparedIngest"}.issubset(
        _class_names(_STORE / "corpus_service.py")
    )
    assert not (_STORE / "service.py").exists()


def test_retrieval_behavior_is_extracted_from_canonical_corpus_implementation() -> None:
    corpus_type = corpus_service.CorpusService
    retrieval_path = _STORE / "retrieval" / "service.py"
    assert issubclass(corpus_type, RetrievalService)
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


def test_internal_corpus_builders_use_final_composition_boundary() -> None:
    composition_source = (_STORE / "composition.py").read_text(encoding="utf-8")
    direct_scrape_application = (
        _STORE / "acquisition" / "direct_scrape_application.py"
    ).read_text(encoding="utf-8")

    assert not (_STORE / "container.py").exists()
    assert not (_STORE / "acquisition" / "direct_scrape.py").exists()
    assert "from .service import CorpusService" not in composition_source
    assert "from .corpus_service import CorpusService" in composition_source
    assert "def build_direct_scrape_service(" in composition_source
    assert "class DirectScrapeService" in direct_scrape_application
    assert "CorpusService" not in direct_scrape_application
    assert not _imports_name(direct_scrape_application, "composition")


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
