from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.cli import parser as research_store_parser
from research_store.execution_policy import (
    ExecutionModeError,
    ExecutionModePolicy,
    SemanticAuthority,
)
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
    assert (
        require_disposable_database_reset(
            "postgresql://research_app@localhost/research_assets_test_codex",
            "1",
        )
        == "research_assets_test_codex"
    )
    assert (
        require_disposable_database_reset(
            "postgresql://research_app@localhost/research_assets_test_codex",
            "",
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


def test_standalone_cli_defaults_to_autonomous_local_mode():
    args = research_store_parser().parse_args(
        ["run-start", "fr_test", "explicit mode default"]
    )
    assert args.mode == "autonomous_local"


@pytest.mark.parametrize(
    ("mode", "host", "fixture", "authority"),
    [
        ("agent_led", True, False, SemanticAuthority.HOST_AGENT),
        ("autonomous_local", False, False, SemanticAuthority.LOCAL_MODEL),
        (
            "deterministic_debug",
            False,
            True,
            SemanticAuthority.DETERMINISTIC_FIXTURE,
        ),
    ],
)
def test_execution_mode_policy_routes_one_explicit_authority(
    mode, host, fixture, authority
):
    policy = ExecutionModePolicy()
    assert (
        policy.route(
            mode,
            host_artifact_supplied=host,
            deterministic_fixture_supplied=fixture,
        )
        == authority
    )


@pytest.mark.parametrize(
    ("mode", "host", "fixture"),
    [
        ("agent_led", False, False),
        ("autonomous_local", True, False),
        ("autonomous_local", False, True),
        ("deterministic_debug", False, False),
    ],
)
def test_execution_mode_policy_rejects_implicit_authority_changes(mode, host, fixture):
    with pytest.raises(ExecutionModeError):
        ExecutionModePolicy().route(
            mode,
            host_artifact_supplied=host,
            deterministic_fixture_supplied=fixture,
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
        ("acquiring", "partial"),
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


def test_search_skips_semantic_embedding_when_active_alias_has_other_model():
    candidate_id = uuid4()

    class Repository:
        documents = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self
            self._execution_records = []
            self._retrieval_events = []

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "lexical fallback"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            self._execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            self._retrieval_events.extend([(run_id, e) for e in events])

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
        embedding_fingerprint="dummy_fingerprint",
    )
    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=WrongAliasIndex(),
        embedder=forbidden_embedder,
    )

    execution, results = service.search_assets("fallback", candidate_limit=5)
    assert [result["candidate_id"] for result in results] == [str(candidate_id)]
    assert results[0]["excerpt"] == "lexical fallback"
    assert execution.executed_mode == "lexical"
    from research_domain.models import MechanicalStatus

    assert execution.mechanical_status == MechanicalStatus.DEGRADED
    assert execution.index_fingerprint == "research_chunks_other_model"
    assert len(execution.warnings) == 1
    assert "expected" in execution.warnings[0]


def test_search_assets_intentional_lexical_mode():
    candidate_id = uuid4()

    class Repository:
        def __init__(self):
            self.documents = self
            self.retrieval_events = self
            self._execution_records = []
            self._retrieval_events = []

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "lexical intentional"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            self._execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            self._retrieval_events.extend([(run_id, e) for e in events])

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="research_chunks_configured_model",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="dummy_fingerprint",
    )

    class BrokenIndex:
        def list_aliases(self):
            raise AssertionError("index should not be called")

        def search(self, *_args):
            raise AssertionError("index should not be called")

    def forbidden_embedder(_query):
        raise AssertionError("embedder should not be called")

    def forbidden_reranker(_query, _candidates):
        raise AssertionError("reranker should not be called")

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=BrokenIndex(),
        embedder=forbidden_embedder,
        reranker=forbidden_reranker,
    )

    execution, results = service.search_assets(
        "fallback", candidate_limit=5, requested_mode="lexical"
    )
    assert [result["candidate_id"] for result in results] == [str(candidate_id)]
    assert results[0]["excerpt"] == "lexical intentional"
    assert execution.executed_mode == "lexical"
    assert "embedding" in execution.skipped_stages
    assert "qdrant" in execution.skipped_stages
    assert "reranker" in execution.skipped_stages
    from research_domain.models import MechanicalStatus

    assert execution.mechanical_status == MechanicalStatus.SUCCEEDED


def test_search_assets_intentional_semantic_mode():
    candidate_id = uuid4()

    class Repository:
        def __init__(self):
            self.documents = self
            self.retrieval_events = self
            self._execution_records = []
            self._retrieval_events = []

        def search_lexical(self, *_args):
            raise AssertionError("lexical search should not be called")

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "semantic intentional"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            self._execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            self._retrieval_events.extend([(run_id, e) for e in events])

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="research_chunks_configured_model",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="dummy_fingerprint",
    )

    class WorkingIndex:
        def list_aliases(self):
            return {"active": "research_chunks_configured_model"}

        def search(self, *_args):
            return [
                {
                    "id": str(uuid4()),
                    "score": 0.9,
                    "payload": {"chunk_id": str(candidate_id), "title": "Test"},
                }
            ]

    def dummy_embedder(_query):
        return [0.1]

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=WorkingIndex(),
        embedder=dummy_embedder,
    )

    execution, results = service.search_assets(
        "test", candidate_limit=5, requested_mode="semantic"
    )
    assert [result["candidate_id"] for result in results] == [str(candidate_id)]
    assert results[0]["excerpt"] == "semantic intentional"
    assert execution.executed_mode == "semantic"
    assert execution.index_fingerprint == "research_chunks_configured_model"
    from research_domain.models import MechanicalStatus

    assert execution.mechanical_status == MechanicalStatus.SUCCEEDED

    # Semantic mode timing and skipped_stages
    assert execution.timing["lexical"] == 0.0
    assert "semantic" in execution.timing
    assert "reranker" in execution.skipped_stages


def test_semantic_mode_with_alias_mismatch_is_failed():
    """Semantic mode with wrong alias should be FAILED, not DEGRADED."""
    from uuid import uuid4

    candidate_id = uuid4()

    class Repository:
        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "semantic alias mismatch"}]

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
        embedding_fingerprint="dummy_fingerprint",
    )
    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=WrongAliasIndex(),
        embedder=forbidden_embedder,
    )

    execution, results = service.search_assets(
        "test", candidate_limit=5, requested_mode="semantic"
    )
    # Semantic mode skips lexical entirely, so a semantic failure produces zero results
    assert len(results) == 0
    assert execution.requested_mode == "semantic"
    assert execution.executed_mode == "lexical"
    from research_domain.models import MechanicalStatus

    assert execution.mechanical_status == MechanicalStatus.FAILED
    # Alias mismatch is a config issue, not a component failure
    assert execution.component_health["qdrant"] == "healthy"
    assert execution.component_health["embedding"] == "healthy"
    assert execution.errors == ()
    assert len(execution.warnings) == 1
    assert "expected" in execution.warnings[0]
    assert "embedding" in execution.skipped_stages
    assert "qdrant" in execution.skipped_stages
    assert execution.index_fingerprint == "research_chunks_other_model"


def test_search_assets_with_run_id_persists_execution_and_events():
    """Verify that when run_id is provided, execution record and retrieval events are persisted."""
    from uuid import uuid4

    candidate_id = uuid4()
    run_id = uuid4()

    _execution_records = []
    _retrieval_events = []

    class Repository:
        documents = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "persistence test"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            _execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            _retrieval_events.extend([(run_id, e) for e in events])

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="research_chunks_configured_model",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="dummy_fingerprint",
    )

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=None,
        embedder=None,
    )

    _execution, results = service.search_assets(
        "persistence", candidate_limit=5, run_id=run_id, requested_mode="lexical"
    )
    assert len(results) == 1
    assert results[0]["candidate_id"] == str(candidate_id)

    # Verify execution record was persisted
    assert len(_execution_records) == 1
    persisted_run_id, persisted_exec = _execution_records[0]
    assert persisted_run_id == run_id
    assert persisted_exec.requested_mode == "lexical"
    assert persisted_exec.executed_mode == "lexical"
    from research_domain.models import MechanicalStatus

    assert persisted_exec.mechanical_status == MechanicalStatus.SUCCEEDED
    assert persisted_exec.index_fingerprint is None

    # Verify retrieval event was logged
    assert len(_retrieval_events) == 2

    event1_run_id, stage1 = _retrieval_events[0]
    assert event1_run_id == run_id
    assert stage1["candidate_id"] == str(candidate_id)
    assert stage1["stage"] == "lexical"
    assert stage1["selected"] is False

    event2_run_id, stage2 = _retrieval_events[1]
    assert event2_run_id == run_id
    assert stage2["candidate_id"] == str(candidate_id)
    assert stage2["stage"] == "fused"
    assert stage2["selected"] is True


def test_search_assets_hybrid_mode_with_qdrant_failure():
    """Hybrid mode (default) with Qdrant failure should degrade to lexical-only."""
    from uuid import uuid4

    candidate_id = uuid4()

    class Repository:
        def __init__(self):
            self.documents = self
            self.retrieval_events = self
            self._execution_records = []
            self._retrieval_events = []

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "hybrid degradation"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            self._execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            self._retrieval_events.extend([(run_id, e) for e in events])

    class BrokenQdrant:
        def list_aliases(self):
            return {"active": "configured"}

        def search(self, *_args):
            raise OSError("qdrant unavailable")

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="configured",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="dummy_fingerprint",
    )

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=BrokenQdrant(),
        embedder=lambda _q: [0.1],
    )

    # Default mode is "hybrid" (requested_mode="hybrid" is not "semantic", so lexical runs)
    execution, results = service.search_assets(
        "hybrid", candidate_limit=5, requested_mode="hybrid"
    )
    assert len(results) == 1
    assert results[0]["candidate_id"] == str(candidate_id)
    assert execution.requested_mode == "hybrid"
    assert execution.executed_mode == "lexical"
    from research_domain.models import MechanicalStatus

    assert execution.mechanical_status == MechanicalStatus.DEGRADED
    assert execution.component_health["qdrant"] == "failed"
    assert execution.component_health["embedding"] == "failed"
    assert "embedding" in execution.skipped_stages
    assert "qdrant" in execution.skipped_stages
    assert execution.index_fingerprint == "configured"


def test_search_assets_cli_output_format():
    """Verify CLI output contains all expected execution fields."""
    from uuid import uuid4

    candidate_id = uuid4()

    class Repository:
        documents = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self
            self._execution_records = []
            self._retrieval_events = []

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "cli test"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            self._execution_records.append((run_id, execution))

        def log_retrieval_batch(self, execution_id, run_id, events):
            self._retrieval_events.extend([(run_id, e) for e in events])

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="research_chunks_configured_model",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="abc123def456",
    )

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=None,
        embedder=None,
    )

    execution, _results = service.search_assets(
        "cli", candidate_limit=5, requested_mode="lexical"
    )

    # Verify execution has all expected fields
    assert hasattr(execution, "execution_id")
    assert hasattr(execution, "run_id")
    assert hasattr(execution, "requested_mode")
    assert hasattr(execution, "executed_mode")
    assert hasattr(execution, "mechanical_status")
    assert hasattr(execution, "component_health")
    assert hasattr(execution, "errors")
    assert hasattr(execution, "warnings")
    assert hasattr(execution, "stage_counts")
    assert hasattr(execution, "index_fingerprint")
    assert hasattr(execution, "filters")
    assert hasattr(execution, "skipped_stages")
    assert hasattr(execution, "timing")
    assert hasattr(execution, "config_identity")

    # Verify component_health has all expected keys
    for component in ("lexical", "embedding", "qdrant", "reranker", "fusion"):
        assert component in execution.component_health

    # Verify timing has expected keys
    assert "lexical" in execution.timing


def test_retrieval_stage_trace_logging():
    """Verify that all ranking stages are logged, logging failure is fatal, and rejection reasons are set."""
    from uuid import uuid4

    import pytest

    candidate_id = uuid4()
    candidate_id_2 = uuid4()
    run_id = uuid4()

    _logged_events = []

    class FailingRepo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return [
                {"candidate_id": candidate_id, "lexical_score": 1.0},
                {"candidate_id": candidate_id_2, "lexical_score": 0.5},
            ]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "trace test"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            pass

        def log_retrieval_batch(self, execution_id, run_id, events):
            _logged_events.extend(events)
            raise RuntimeError("Intentional logging failure")

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=40,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    service = CorpusService(
        config,
        FailingRepo,
        blob_store=None,
        index=None,
        embedder=None,
    )

    # Should crash because log_retrieval_batch raises RuntimeError
    with pytest.raises(RuntimeError, match="Intentional logging failure"):
        _execution, _results = service.search_assets(
            "trace", candidate_limit=1, run_id=run_id, requested_mode="lexical"
        )

    # Check that events were generated correctly before the simulated crash
    assert len(_logged_events) == 4
    assert _logged_events[0]["stage"] == "lexical"
    assert _logged_events[0]["candidate_id"] == str(candidate_id)
    assert _logged_events[0]["selected"] is False
    assert _logged_events[1]["stage"] == "lexical"
    assert _logged_events[1]["candidate_id"] == str(candidate_id_2)
    assert _logged_events[1]["selected"] is False

    assert _logged_events[2]["stage"] == "fused"
    assert _logged_events[2]["candidate_id"] == str(candidate_id)
    assert _logged_events[2]["selected"] is True
    assert _logged_events[2]["rejection_reason"] is None

    assert _logged_events[3]["stage"] == "fused"
    assert _logged_events[3]["candidate_id"] == str(candidate_id_2)
    assert _logged_events[3]["selected"] is False
    assert _logged_events[3]["rejection_reason"] == "below_candidate_limit"


def test_get_retrieval_trace_api():
    """Verify get_retrieval_trace returns ordered events per execution."""
    from uuid import uuid4

    candidate_id = uuid4()
    run_id = uuid4()

    _trace_events = []
    _execution_id_holder = []

    class Repo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return [{"candidate_id": candidate_id, "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return [{"chunk_id": candidate_id, "text": "trace test"}]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            _execution_id_holder.append(execution.execution_id)

        def log_retrieval_batch(self, execution_id, run_id, events):
            _trace_events.extend(events)

        def get_trace(self, exec_id):
            # Simulate the actual SQL ordering: stage priority then rank
            stage_order = {"lexical": 1, "semantic": 2, "fused": 3, "reranked": 4}
            return sorted(
                [
                    {
                        "stage": e["stage"],
                        "query": e.get("query"),
                        "filters": e.get("filters"),
                        "retriever": e.get("retriever"),
                        "candidate_type": e.get("candidate_type"),
                        "candidate_id": e.get("candidate_id"),
                        "raw_score": e.get("raw_score"),
                        "normalized_score": e.get("normalized_score"),
                        "rank": e.get("rank"),
                        "reranker_score": e.get("reranker_score"),
                        "selected": e.get("selected"),
                        "rejection_reason": e.get("rejection_reason"),
                    }
                    for e in _trace_events
                    if e.get("_execution_id") == exec_id
                    or True  # all events for this mock
                ],
                key=lambda e: (stage_order.get(e["stage"], 99), e.get("rank", 0)),
            )

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=40,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    service = CorpusService(
        config,
        Repo,
        blob_store=None,
        index=None,
        embedder=None,
    )

    _execution, _results = service.search_assets(
        "trace", candidate_limit=1, run_id=run_id, requested_mode="lexical"
    )

    assert len(_execution_id_holder) == 1
    actual_exec_id = _execution_id_holder[0]

    trace = service.get_retrieval_trace(actual_exec_id)
    assert len(trace) == 2
    # Verify stage ordering: lexical before fused
    assert trace[0]["stage"] == "lexical"
    assert trace[1]["stage"] == "fused"
    # Verify field mapping
    assert trace[0]["candidate_id"] == str(candidate_id)
    assert trace[0]["raw_score"] == 1.0
    assert trace[0]["selected"] is False
    assert trace[1]["selected"] is True


def test_get_retrieval_trace_empty():
    """Verify get_retrieval_trace returns empty list for non-existent execution."""
    from uuid import uuid4

    class Repo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return []

        def fetch_passages(self, *_args):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            pass

        def log_retrieval_batch(self, execution_id, run_id, events):
            pass

        def get_trace(self, exec_id):
            return []

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=40,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    service = CorpusService(
        config,
        Repo,
        blob_store=None,
        index=None,
        embedder=None,
    )

    trace = service.get_retrieval_trace(uuid4())
    assert trace == []


def test_log_retrieval_batch_run_status_guard():
    """Verify log_retrieval_batch raises KeyError when run is not 'running'."""
    from uuid import uuid4

    class Repo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return [{"candidate_id": uuid4(), "lexical_score": 1.0}]

        def fetch_passages(self, *_args):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            pass

        def log_retrieval_batch(self, execution_id, run_id, events):
            # Simulate the authoritative nonterminal-state guard returning zero rows.
            raise KeyError(
                f"research run is absent or finished: {run_id} "
                f"(expected {len(events)} rows, got 0)"
            )

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=40,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    service = CorpusService(
        config,
        Repo,
        blob_store=None,
        index=None,
        embedder=None,
    )

    run_id = uuid4()
    with pytest.raises(KeyError, match="research run is absent or finished"):
        service.search_assets(
            "trace", candidate_limit=5, run_id=run_id, requested_mode="lexical"
        )


def test_reranked_stage_events_with_fused_intermediate():
    """Verify trace includes fused (below_reranker_limit) and reranked (below_candidate_limit) events."""
    from uuid import uuid4

    candidate_id = uuid4()
    candidate_id_2 = uuid4()
    candidate_id_3 = uuid4()
    run_id = uuid4()

    _logged_events = []

    class Repo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return []

        def fetch_passages(self, *_args):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            pass

        def log_retrieval_batch(self, execution_id, run_id, events):
            _logged_events.extend(events)

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=2,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    class MockIndex:
        def list_aliases(self):
            return {"active": "test"}

        def search(self, *_args):
            return [
                {
                    "id": str(uuid4()),
                    "score": 0.9,
                    "payload": {"chunk_id": str(candidate_id), "title": "Test"},
                },
                {
                    "id": str(uuid4()),
                    "score": 0.8,
                    "payload": {"chunk_id": str(candidate_id_2), "title": "Test 2"},
                },
                {
                    "id": str(uuid4()),
                    "score": 0.7,
                    "payload": {"chunk_id": str(candidate_id_3), "title": "Test 3"},
                },
            ]

    class MockReranker:
        def __call__(self, query, candidates):
            # Re-rank and return all candidates with reranker_score
            return [
                {
                    "candidate_id": str(candidate_id),
                    "reranker_score": 0.9,
                    "fused_score": 0.8,
                    "semantic_score": 0.9,
                },
                {
                    "candidate_id": str(candidate_id_2),
                    "reranker_score": 0.7,
                    "fused_score": 0.6,
                    "semantic_score": 0.8,
                },
                {
                    "candidate_id": str(candidate_id_3),
                    "reranker_score": 0.5,
                    "fused_score": 0.4,
                    "semantic_score": 0.7,
                },
            ]

    service = CorpusService(
        config,
        Repo,
        blob_store=None,
        index=MockIndex(),
        embedder=lambda q: [0.1],
        reranker=MockReranker(),
    )

    _execution, results = service.search_assets(
        "rerank test", candidate_limit=1, run_id=run_id, requested_mode="semantic"
    )

    # Should have 3 fused events and 3 reranked events
    fused_events = [e for e in _logged_events if e["stage"] == "fused"]
    reranked_events = [e for e in _logged_events if e["stage"] == "reranked"]

    assert len(fused_events) == 3
    assert len(reranked_events) == 3

    # Fused events: fused_is_final is False (reranker succeeds), limit=reranker_candidate_limit=2
    assert fused_events[0]["rejection_reason"] is None
    assert fused_events[1]["rejection_reason"] is None
    assert fused_events[2]["rejection_reason"] == "below_reranker_limit"

    # Reranked events: limit=candidate_limit=1
    assert reranked_events[0]["selected"] is True
    assert reranked_events[0]["rejection_reason"] is None
    assert reranked_events[1]["selected"] is False
    assert reranked_events[1]["rejection_reason"] == "below_candidate_limit"
    assert reranked_events[2]["selected"] is False
    assert reranked_events[2]["rejection_reason"] == "below_candidate_limit"

    # Final results should only have 1 candidate
    assert len(results) == 1
    assert results[0]["candidate_id"] == str(candidate_id)


def test_search_assets_empty_input_no_events_logged():
    """Verify that when both lexical and semantic are empty, no stage events are logged."""
    from uuid import uuid4

    run_id = uuid4()

    _logged_events = []

    class Repo:
        documents = None
        retrieval_events = None

        def __init__(self):
            self.documents = self
            self.retrieval_events = self

        def search_lexical(self, *_args):
            return []

        def fetch_passages(self, *_args):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def record_retrieval_execution(self, run_id, execution):
            pass

        def log_retrieval_batch(self, execution_id, run_id, events):
            _logged_events.extend(events)

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test",
        reranker_candidate_limit=40,
        parser_version="v1",
        normalization_version="v1",
        chunker_version="v1",
        embedding_fingerprint="abc",
    )

    service = CorpusService(
        config,
        Repo,
        blob_store=None,
        index=None,
        embedder=None,
    )

    execution, results = service.search_assets(
        "empty", candidate_limit=5, run_id=run_id, requested_mode="lexical"
    )

    assert len(results) == 0
    assert len(_logged_events) == 0
    assert execution.mechanical_status.name == "SUCCEEDED"
