from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store import qdrant_authority
from research_store.qdrant_authority import evaluate_required_alias_state


def _definition(identifier="def-1", collection="research_chunks_target"):
    return {
        "id": identifier,
        "physical_collection": collection,
        "dimension": 1024,
        "distance_metric": "Cosine",
    }


def _projection_snapshot(points_count: int) -> dict:
    return {
        "required_alias_name": "research_chunks_active",
        "target_collection": "research_chunks_target",
        "postgres_active_definition": "def-1",
        "dimension": 1024,
        "distance_metric": "Cosine",
        "schema_actual": {"size": 1024, "distance": "Cosine", "sparse": False},
        "schema_expected": {"size": 1024, "distance": "Cosine", "sparse": False},
        "points_count": points_count,
        "sample_ids": (),
    }


def test_required_alias_state_requires_exact_configured_alias():
    state = evaluate_required_alias_state(
        aliases={"some_other_alias": "research_chunks_target"},
        required_alias_name="research_chunks_active",
        active_definitions=[_definition()],
    )
    assert state["status"] == "missing_required_alias"
    assert state["actual_required_alias_target"] is None


def test_required_alias_state_rejects_wrong_target():
    state = evaluate_required_alias_state(
        aliases={"research_chunks_active": "research_chunks_wrong"},
        required_alias_name="research_chunks_active",
        active_definitions=[_definition()],
    )
    assert state["status"] == "wrong_required_alias_target"
    assert state["expected_active_collection"] == "research_chunks_target"


def test_required_alias_state_accepts_exact_target():
    state = evaluate_required_alias_state(
        aliases={"research_chunks_active": "research_chunks_target"},
        required_alias_name="research_chunks_active",
        active_definitions=[_definition()],
    )
    assert state["status"] == "healthy"
    assert state["postgres_active_definition"] == "def-1"
    assert state["dimension"] == 1024
    assert state["distance_metric"] == "Cosine"


def test_required_alias_state_fails_closed_without_active_definition():
    state = evaluate_required_alias_state(
        aliases={"research_chunks_active": "research_chunks_target"},
        required_alias_name="research_chunks_active",
        active_definitions=[],
    )
    assert state["status"] == "no_active_definition"
    assert state["postgres_active_definition"] is None


def test_required_alias_state_fails_closed_on_multiple_active_definitions():
    state = evaluate_required_alias_state(
        aliases={"research_chunks_active": "research_chunks_target"},
        required_alias_name="research_chunks_active",
        active_definitions=[
            _definition("def-1", "research_chunks_target"),
            _definition("def-2", "research_chunks_other"),
        ],
    )
    assert state["status"] == "multiple_active_definitions"
    assert state["postgres_active_definition"] is None
    assert state["postgres_active_definitions"] == ["def-1", "def-2"]


def test_required_alias_state_requires_definition_shape():
    with pytest.raises(KeyError):
        evaluate_required_alias_state(
            aliases={},
            required_alias_name="research_chunks_active",
            active_definitions=[{"id": "def-1"}],
        )


@pytest.mark.parametrize("after_count", [9, 11])
def test_projection_preservation_requires_exact_point_count(monkeypatch, after_count):
    before = _projection_snapshot(10)
    after = _projection_snapshot(after_count)
    monkeypatch.setattr(
        qdrant_authority,
        "capture_configured_projection_state",
        lambda _config: after,
    )

    with pytest.raises(RuntimeError, match="point count changed during probe cleanup"):
        qdrant_authority.require_configured_projection_preserved(object(), before)


def test_projection_preservation_accepts_exact_point_count(monkeypatch):
    before = _projection_snapshot(10)
    monkeypatch.setattr(
        qdrant_authority,
        "capture_configured_projection_state",
        lambda _config: dict(before),
    )

    assert (
        qdrant_authority.require_configured_projection_preserved(object(), before)
        == before
    )
