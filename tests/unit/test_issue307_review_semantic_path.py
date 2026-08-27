"""Independent-review regressions for the canonical semantic-objective path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.smart_objective_intent import (
    interpret_smart_objective,
    materialize_smart_objective_intent,
)

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
AUDITED_OBJECTIVE = (
    "Latest serious news and reporting within the past 5 days about Donald Trump "
    "and Iran, prioritizing primary sources and independent corroboration."
)


def test_exact_audited_objective_uses_production_structured_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import smart_objective_intent as intent_module

    semantic_call_id = uuid4()
    artifact_id = uuid4()
    observed: dict[str, Any] = {}
    payload = {
        "schema_version": "smart-objective-intent-v1",
        "objective": AUDITED_OBJECTIVE,
        "research_questions": [
            "What material developments involving Donald Trump and Iran occurred or were authoritatively updated within the past five days?",
            "Which developments are corroborated by primary or independent authoritative sources?",
        ],
        "entities": ["Donald Trump", "Iran"],
        "jurisdictions": ["United States", "Iran"],
        "user_constraints": [
            "Prioritize primary sources and independent corroboration.",
            "Use serious news and reporting.",
        ],
        "temporal": {
            "kind": "relative_freshness",
            "relative_quantity": 5,
            "relative_unit": "day",
            "freshness_basis": "publication_or_update",
            "publication_start": None,
            "publication_end": None,
            "uncertainty": "none",
            "rationale": "latest plus past five days expresses rolling freshness",
        },
        "assumptions": [],
        "ambiguities": [],
    }

    semantic_service = SimpleNamespace(host_artifact_supplier=None)

    def fake_authorized_structured(**kwargs: Any) -> SimpleNamespace:
        observed["semantic_service"] = kwargs["semantic_service"]
        observed["user_prompt"] = json.loads(kwargs["user_prompt"])
        observed["schema"] = kwargs["schema"]
        observed["semantic_context"] = kwargs["semantic_context"]
        kwargs["post_validate"](payload)
        return SimpleNamespace(
            value=payload,
            error=None,
            provenance={"provider": "local-test"},
            semantic_call_id=semantic_call_id,
            artifact_ids=(artifact_id,),
        )

    monkeypatch.setattr(
        intent_module,
        "call_authorized_structured",
        fake_authorized_structured,
    )

    status = SimpleNamespace(
        id=uuid4(),
        lifecycle_revision=0,
        state="created",
        execution_mode="autonomous_local",
    )
    interpreted = interpret_smart_objective(
        semantic_service=semantic_service,
        status=status,
        objective=AUDITED_OBJECTIVE,
        invocation_id="issue307-review-production-semantic-path",
        evaluated_at=CLOCK,
    )
    materialized = materialize_smart_objective_intent(
        interpreted.value,
        execution_mode=status.execution_mode,
        evaluated_at=CLOCK,
    )
    serialized = serialize_model(materialized.spec)

    assert observed["semantic_service"] is semantic_service
    assert observed["user_prompt"] == {"objective": AUDITED_OBJECTIVE}
    assert observed["schema"]["additionalProperties"] is False
    assert observed["semantic_context"]["stage"] == "smart_objective_intent"
    assert serialized["objective"] == AUDITED_OBJECTIVE
    assert serialized["time_window"]["start"] is None
    assert serialized["time_window"]["end"] is None
    assert serialized["freshness_requirements"][0]["max_age_days"] == 5
    assert (
        materialized.discovery_window.start == (CLOCK - timedelta(days=5)).isoformat()
    )
    assert materialized.discovery_window.end == CLOCK.isoformat()
    assert interpreted.provenance["provider"] == "local-test"
    assert interpreted.semantic_call_id == semantic_call_id
    assert list(interpreted.artifact_ids) == [artifact_id]
