from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import research_store.retrieval as retrieval_module
from model_gateway import StructuredResult
from research_store.release import preflight
from research_store.retrieval import CohereCompatibleReranker


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_production_reranker_adapter_consumes_relevance_score(monkeypatch):
    monkeypatch.setattr(
        retrieval_module,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }
        ),
    )
    candidates = [
        {"candidate_id": "first", "excerpt": "first"},
        {"candidate_id": "second", "excerpt": "second"},
    ]
    result = CohereCompatibleReranker("http://reranker/v1/rerank", "rerank")(
        "second", candidates
    )
    assert [item["candidate_id"] for item in result] == ["second", "first"]
    assert [item["reranker_score"] for item in result] == [0.9, 0.2]


def _configure_preflight_reranker(
    monkeypatch: pytest.MonkeyPatch, scores: list[object]
) -> None:
    monkeypatch.setattr(
        preflight,
        "_config",
        lambda **_kwargs: SimpleNamespace(
            reranker_url="http://reranker/v1/rerank",
            reranker_model="rerank",
            reranker_api_key="",
        ),
    )

    class Reranker:
        def __init__(self, *_args):
            pass

        def __call__(self, _query, _candidates):
            return [{"reranker_score": score} for score in scores]

    monkeypatch.setattr(preflight, "CohereCompatibleReranker", Reranker)


def test_probe_reranker_accepts_finite_descending_numeric_scores(monkeypatch):
    _configure_preflight_reranker(monkeypatch, [0.9, 0.2])
    assert preflight.probe_reranker().endswith("(2 documents)")


@pytest.mark.parametrize(
    ("scores", "error_type", "message"),
    [
        ([0.9, None], TypeError, "non-numeric score"),
        ([0.9, True], TypeError, "non-numeric score"),
        ([0.9, float("nan")], RuntimeError, "non-finite scores"),
        ([0.2, 0.9], RuntimeError, "not ordered by score"),
    ],
)
def test_probe_reranker_rejects_invalid_score_contract(
    monkeypatch: pytest.MonkeyPatch,
    scores: list[object],
    error_type: type[Exception],
    message: str,
):
    _configure_preflight_reranker(monkeypatch, scores)
    with pytest.raises(error_type, match=message):
        preflight.probe_reranker()


def test_redact_url_credentials_masks_password_without_changing_endpoint():
    assert (
        preflight._redact_url_credentials(
            "redis://research_app:p%40ss@127.0.0.1:56379/0"
        )
        == "redis://research_app:***@127.0.0.1:56379/0"
    )
    assert (
        preflight._redact_url_credentials("redis://127.0.0.1:56379/0")
        == "redis://127.0.0.1:56379/0"
    )


def test_probe_qdrant_uses_active_alias_and_named_dense_vector(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    class Index:
        def __init__(self, *_args):
            pass

        def list_aliases(self):
            return {"research_chunks_active": "research_chunks_expected"}

        def for_collection(self, collection, dimension, distance):
            calls.append(("collection", collection, dimension, distance))
            return self

        def inspect_schema(self):
            return {"exists": True, "compatible": True}

        def upsert(self, points):
            calls.append(("upsert", points))

        def retrieve(self, ids):
            point_id = str(ids[0])
            return [{"id": point_id, "payload": {"preflight_probe_id": point_id}}]

        def delete(self, ids):
            calls.append(("delete", list(ids)))

    monkeypatch.setattr(
        preflight,
        "_config",
        lambda **_kwargs: SimpleNamespace(
            qdrant_alias="research_chunks_active",
            embedding_dimension=2,
            physical_collection="research_chunks_expected",
        ),
    )
    monkeypatch.setattr(preflight, "QdrantIndex", Index)

    assert "write/read/delete OK" in preflight.probe_qdrant(
        "http://qdrant", "", [1.0, 0.0]
    )
    upsert = next(call[1] for call in calls if call[0] == "upsert")
    assert upsert[0]["vector"] == {"dense": [1.0, 0.0]}
    assert any(call[0] == "delete" for call in calls)


def test_probe_generative_requires_exact_schema_bound_probe_id(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "_config",
        lambda **_kwargs: SimpleNamespace(
            generative_url="http://generative/v1",
            generative_model="chat",
        ),
    )

    def call_structured(**kwargs):
        probe_id = kwargs["schema"]["properties"]["probe_id"]["const"]
        assert kwargs["schema"]["additionalProperties"] is False
        return StructuredResult(
            {"status": "ok", "probe_id": probe_id},
            {},
            (),
        )

    monkeypatch.setattr(preflight.model_gateway, "call_structured", call_structured)
    assert preflight.probe_generative().endswith("request OK")


def test_probe_resources_requires_measured_cpu_and_gpu(monkeypatch):
    measured_cpu = SimpleNamespace(status="measured", value=12.5)
    measured_gpu = SimpleNamespace(
        status="measured", value=1024.0, device_uuid="GPU-test", failure_reason=""
    )

    class Sampler:
        def __init__(self, **_kwargs):
            pass

        def begin_window(self):
            pass

        def end_window(self):
            return [measured_cpu], [measured_gpu]

    monkeypatch.setattr(preflight, "ResourceSampler", Sampler)
    message = preflight.probe_resources()
    assert "CPU=12.5" in message
    assert "GPU=1024.0" in message


def test_complete_preflight_collects_failures_without_skipping_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dataset = tmp_path / "benchmark.json"
    dataset.write_text("{}", encoding="utf-8")
    worker = mock.Mock(return_value="Worker OK")

    monkeypatch.setattr(
        preflight,
        "probe_postgres",
        mock.Mock(side_effect=RuntimeError("index worker heartbeat is stale")),
    )
    monkeypatch.setattr(preflight, "probe_valkey", mock.Mock(return_value="Valkey OK"))
    monkeypatch.setattr(
        preflight, "probe_firecrawl", mock.Mock(return_value="Firecrawl OK")
    )
    monkeypatch.setattr(
        preflight,
        "probe_embedding",
        mock.Mock(return_value=("Embedding OK", [1.0, 0.0])),
    )
    monkeypatch.setattr(
        preflight,
        "probe_qdrant",
        mock.Mock(side_effect=RuntimeError("active alias missing")),
    )
    monkeypatch.setattr(
        preflight, "probe_reranker", mock.Mock(return_value="Reranker OK")
    )
    monkeypatch.setattr(
        preflight, "probe_generative", mock.Mock(return_value="Generative OK")
    )
    monkeypatch.setattr(
        preflight, "probe_resources", mock.Mock(return_value="Collectors OK")
    )
    monkeypatch.setattr(preflight, "probe_index_worker", worker)
    monkeypatch.setenv("VALKEY_URL", "redis://valkey/0")

    ok, errors = preflight.run_complete_preflight(
        database_url="postgresql://test",
        blob_root=tmp_path / "blobs",
        qdrant_url="http://qdrant",
        qdrant_api_key="",
        dataset_path=dataset,
        campaign_dir=tmp_path / "campaign",
        candidate_sha="a" * 40,
        get_full_sha=lambda: "a" * 40,
    )

    assert ok is False
    assert len(errors) == 2
    assert any("heartbeat" in error for error in errors)
    assert any("alias" in error for error in errors)
    worker.assert_called_once()
