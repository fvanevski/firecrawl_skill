from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.cli import parser as research_store_parser
from research_store.execution_policy import (
    ExecutionModeError,
    ExecutionModePolicy,
    SemanticAuthority,
)
from research_store.legacy_adapter import (
    AdapterMode,
    LegacyAdapterError,
    LegacyEntryPointAdapter,
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


def test_legacy_adapter_compatibility_mode_preserves_cli_without_uow():
    result = LegacyEntryPointAdapter(None, AdapterMode.COMPATIBILITY).route(
        "fsearch",
        {"action": "search", "status": "complete", "input": {"query": "x"}},
        external_invocation_id="fc_test",
        idempotency_key="compat:test",
    )
    assert result.recorded is False
    assert result.authoritative_write is False
    assert result.service_operation == "acquisition.single_query"


class AdapterRepository:
    def __init__(self, fail=False):
        self.fail = fail
        self.comparisons = []
        self.invocations = []
        self.events = []

    def get_run_status(self, *, external_id=None, run_id=None):
        return {
            "id": "run-id",
            "external_id": external_id,
            "lifecycle_revision": 7,
        }

    def record_legacy_adapter_comparison(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.comparisons.append((args, kwargs))
        return "comparison-id"

    def record_invocation(self, *args, **kwargs):
        self.invocations.append((args, kwargs))
        return "invocation-id"

    def append_event(self, *args, **kwargs):
        self.events.append((args, kwargs))
        return "event-id"


class AdapterUnitOfWork:
    def __init__(self, repository):
        self.runs = repository

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_shadow_adapter_records_comparison_without_workflow_side_effects():
    repository = AdapterRepository()
    adapter = LegacyEntryPointAdapter(
        lambda: AdapterUnitOfWork(repository), AdapterMode.SHADOW
    )
    result = adapter.route(
        "fsearch_smart",
        {"action": "orchestrate", "status": "complete", "input": {"topic": "x"}},
        external_run_id="fr_test",
        external_invocation_id="fc_test",
        idempotency_key="shadow:test",
    )
    assert result.recorded is True
    assert result.authoritative_write is False
    assert len(repository.comparisons) == 1
    assert repository.invocations == []
    assert repository.events == []


def test_shadow_adapter_marks_service_behavior_divergence_queryably():
    repository = AdapterRepository()
    adapter = LegacyEntryPointAdapter(
        lambda: AdapterUnitOfWork(repository), AdapterMode.SHADOW
    )
    result = adapter.route(
        "fsearch",
        {"action": "search", "status": "complete", "input": {"query": "x"}},
        service_proposal={"action": "retrieve"},
        external_run_id="fr_test",
        external_invocation_id="fc_test",
        idempotency_key="shadow:divergent",
    )
    assert result.divergent is True
    assert result.divergence_reasons == ("action",)


def test_authoritative_adapter_routes_to_invocation_service_boundary():
    repository = AdapterRepository()
    adapter = LegacyEntryPointAdapter(
        lambda: AdapterUnitOfWork(repository), AdapterMode.AUTHORITATIVE
    )
    result = adapter.route(
        "fscrape",
        {"action": "scrape", "status": "complete", "input": {"urls": ["x"]}},
        external_run_id="fr_test",
        external_invocation_id="fc_test",
        idempotency_key="authoritative:test",
    )
    assert result.authoritative_write is True
    assert len(repository.invocations) == 1
    assert len(repository.events) == 1
    assert len(repository.comparisons) == 1


def test_adapter_failure_propagates_without_silent_compatibility_fallback():
    repository = AdapterRepository(fail=True)
    adapter = LegacyEntryPointAdapter(
        lambda: AdapterUnitOfWork(repository), AdapterMode.SHADOW
    )
    with pytest.raises(LegacyAdapterError, match="database unavailable"):
        adapter.route(
            "fsearch",
            {"action": "search", "status": "complete", "input": {}},
            external_run_id="fr_test",
            external_invocation_id="fc_test",
            idempotency_key="failure:test",
        )
    assert repository.invocations == []
    assert repository.events == []


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
    assert policy.route(
        mode,
        host_artifact_supplied=host,
        deterministic_fixture_supplied=fixture,
    ) == authority


@pytest.mark.parametrize(
    ("mode", "host", "fixture"),
    [
        ("agent_led", False, False),
        ("autonomous_local", True, False),
        ("autonomous_local", False, True),
        ("deterministic_debug", False, False),
    ],
)
def test_execution_mode_policy_rejects_implicit_authority_changes(
    mode, host, fixture
):
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

    execution, results = service.search_assets("fallback", candidate_limit=5, requested_mode="lexical")
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
            return [{"id": str(uuid4()), "score": 0.9, "payload": {"chunk_id": str(candidate_id), "title": "Test"}}]

    def dummy_embedder(_query):
        return [0.1]

    service = CorpusService(
        config,
        Repository,
        blob_store=None,
        index=WorkingIndex(),
        embedder=dummy_embedder,
    )

    execution, results = service.search_assets("test", candidate_limit=5, requested_mode="semantic")
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

    execution, results = service.search_assets(
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

    execution, results = service.search_assets(
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
    """Verify that all ranking stages are logged and logging failure is non-fatal."""
    from uuid import uuid4
    candidate_id = uuid4()
    run_id = uuid4()

    _logged_events = []

    class FailingRepo:
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

    # Should not crash despite log_retrieval_batch raising RuntimeError
    execution, results = service.search_assets(
        "trace", candidate_limit=5, run_id=run_id, requested_mode="lexical"
    )

    assert len(results) == 1
    # Check that events were generated correctly before the simulated crash
    assert len(_logged_events) == 2
    assert _logged_events[0]["stage"] == "lexical"
    assert _logged_events[0]["selected"] is False
    assert _logged_events[1]["stage"] == "fused"
    assert _logged_events[1]["selected"] is True

    assert "fusion" in execution.timing
    assert "fetch_passages" in execution.timing

    # Verify stage_counts
    assert "lexical" in execution.stage_counts
    assert "semantic" in execution.stage_counts
    assert "fused" in execution.stage_counts

