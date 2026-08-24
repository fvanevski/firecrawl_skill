"""Independent-review regressions for the production smart-objective path."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain import serialize_model

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
AUDITED_OBJECTIVE = (
    "Latest serious news and reporting within the past 5 days about Donald Trump "
    "and Iran, prioritizing primary sources and independent corroboration."
)


def _load_fsearch_smart() -> Any:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    loader = SourceFileLoader(
        "issue307_review_fsearch_smart", str(scripts / "fsearch_smart")
    )
    spec = importlib.util.spec_from_loader("issue307_review_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_exact_audited_objective_uses_production_structured_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import (
        semantic_service as semantic_service_module,
    )
    from firecrawl_skill.research_store import smart_objective_intent as intent_module

    smart = _load_fsearch_smart()
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

    class _SemanticService:
        def __init__(
            self,
            uow_factory: Any,
            host_artifact_supplier: Any = None,
        ) -> None:
            observed["uow_factory"] = uow_factory
            observed["host_artifact_supplier"] = host_artifact_supplier
            self.host_artifact_supplier = host_artifact_supplier

    def fake_authorized_structured(**kwargs: Any) -> SimpleNamespace:
        observed["user_prompt"] = json.loads(kwargs["user_prompt"])
        observed["schema"] = kwargs["schema"]
        observed["semantic_context"] = kwargs["semantic_context"]
        post_validate = kwargs["post_validate"]
        post_validate(payload)
        return SimpleNamespace(
            value=payload,
            error=None,
            provenance={"provider": "local-test"},
            semantic_call_id=semantic_call_id,
            artifact_ids=(artifact_id,),
        )

    monkeypatch.setattr(
        semantic_service_module,
        "SemanticCallService",
        _SemanticService,
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
    uow_factory = lambda: None
    run_service = SimpleNamespace(
        uow_factory=uow_factory,
        host_artifact_supplier=None,
    )

    spec, discovery, source, provenance = smart.resolve_objective_spec(
        path=None,
        topic=AUDITED_OBJECTIVE,
        status=status,
        run_service=run_service,
        invocation_id="issue307-review-production-semantic-path",
        evaluated_at=CLOCK,
    )
    serialized = serialize_model(spec)

    assert observed["uow_factory"] is uow_factory
    assert observed["user_prompt"] == {"objective": AUDITED_OBJECTIVE}
    assert observed["schema"]["additionalProperties"] is False
    assert observed["semantic_context"]["stage"] == "smart_objective_intent"
    assert serialized["objective"] == AUDITED_OBJECTIVE
    assert serialized["time_window"]["start"] is None
    assert serialized["time_window"]["end"] is None
    assert serialized["freshness_requirements"][0]["max_age_days"] == 5
    assert discovery.start == (CLOCK - timedelta(days=5)).isoformat()
    assert discovery.end == CLOCK.isoformat()
    assert source == "semantic objective intent"
    assert provenance["semantic_call_id"] == str(semantic_call_id)
    assert provenance["artifact_ids"] == [str(artifact_id)]
