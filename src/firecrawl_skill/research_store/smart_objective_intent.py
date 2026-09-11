"""Structured smart-objective interpretation and deterministic materialization.

The local model may interpret natural-language semantics, but it never owns time
arithmetic, provider parameters, ResearchSpec construction, or temporal evidence
qualification. The structured semantic artifact is persisted through the
existing SemanticCallService before this module deterministically materializes
its consequences.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firecrawl_skill.research_domain.models import (
    ExecutionMode,
    FreshnessRequirement,
    ResearchQuestion,
    ResearchSpec,
    TemporalBasis,
    TimeWindow,
)

from .authorized_semantic import call_authorized_structured
from .budget_policy import conservative_research_spec
from .fallback_temporal_spec import materialize_smart_fallback_spec
from .temporal_policy import parse_bound

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "research-workflow"
    / "smart-objective-intent-v2.json"
)
SMART_OBJECTIVE_INTENT_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
SMART_OBJECTIVE_INTENT_PROMPT_VERSION = "smart-objective-intent-v5"
_RELATIVE_PUBLICATION_OR_UPDATE = re.compile(
    r"\b(?:publication|published)\s+or\s+(?:update|updated|modification|modified)\s+"
    r"(?:within\s+)?(?:the\s+)?(?:last|past)\s+[1-9]\d*\s+(?:days?|weeks?)\b",
    re.IGNORECASE,
)
_RELATIVE_PUBLICATION_ONLY = re.compile(
    r"\b(?:published|publication)\s+(?:within|in)\s+(?:the\s+)?"
    r"(?:last|past)\s+[1-9]\d*\s+(?:days?|weeks?)\b",
    re.IGNORECASE,
)


class SmartObjectiveIntentError(ValueError):
    """A semantic intent artifact cannot be deterministically materialized."""


@dataclass(frozen=True)
class SmartObjectiveMaterialization:
    spec: ResearchSpec
    discovery_window: TimeWindow
    intent: dict[str, Any]


def _clock(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SmartObjectiveIntentError(
            "smart-objective evaluation clock must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _days(quantity: int, unit: str) -> int:
    if quantity < 1:
        raise SmartObjectiveIntentError("relative temporal quantity must be positive")
    if unit == "day":
        return quantity
    if unit == "week":
        return quantity * 7
    raise SmartObjectiveIntentError(f"unsupported relative temporal unit: {unit}")


def _freshness_id(spec: ResearchSpec, description: str):
    namespace = uuid5(NAMESPACE_URL, str(spec.research_spec_id))
    return uuid5(namespace, f"smart-objective-intent\0{description}")


def _semantic_id(spec: ResearchSpec, kind: str, index: int, value: str):
    namespace = uuid5(NAMESPACE_URL, str(spec.research_spec_id))
    return uuid5(namespace, f"smart-objective-intent\0{kind}\0{index}\0{value}")


def unbounded_discovery_window() -> TimeWindow:
    """Return the canonical provider-neutral unbounded discovery requirement."""

    return TimeWindow(None, None, "no bounded discovery recency", "none")


def _validate_absolute_bounds(
    start_raw: Any, end_raw: Any, *, label: str = "publication"
) -> tuple[str, str]:
    if not isinstance(start_raw, str) or not start_raw.strip():
        raise SmartObjectiveIntentError(f"{label}_start is required")
    if not isinstance(end_raw, str) or not end_raw.strip():
        raise SmartObjectiveIntentError(f"{label}_end is required")
    try:
        start = parse_bound(start_raw)
        end = parse_bound(end_raw, end_of_day=True)
    except (TypeError, ValueError) as exc:
        raise SmartObjectiveIntentError(
            f"{label} bounds must be deterministic ISO-8601 dates or datetimes"
        ) from exc
    if start > end:
        raise SmartObjectiveIntentError(f"{label}_start must not be after {label}_end")
    return start_raw, end_raw


def _validate_as_of(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SmartObjectiveIntentError("as_of is required")
    try:
        parse_bound(value, end_of_day=True)
    except (TypeError, ValueError) as exc:
        raise SmartObjectiveIntentError(
            "as_of must be a deterministic ISO-8601 date or datetime"
        ) from exc
    return value


def _text_list(
    payload: Mapping[str, Any],
    name: str,
    *,
    require_one: bool = False,
) -> tuple[str, ...]:
    values = payload.get(name)
    if not isinstance(values, list):
        raise SmartObjectiveIntentError(f"semantic intent {name} must be an array")
    if require_one and not values:
        raise SmartObjectiveIntentError(
            "semantic intent requires at least one research question"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise SmartObjectiveIntentError(
                f"semantic intent {name} must contain strings"
            )
        text = " ".join(value.split())
        if not text:
            raise SmartObjectiveIntentError(
                f"semantic intent {name} must not contain blank values"
            )
        key = text.casefold()
        if key in seen:
            raise SmartObjectiveIntentError(
                f"semantic intent {name} must not contain duplicate values"
            )
        seen.add(key)
        normalized.append(text)
    return tuple(normalized)


def validate_smart_objective_intent(
    payload: Mapping[str, Any], *, objective: str
) -> None:
    """Enforce cross-field semantics that JSON Schema cannot express."""

    if payload.get("schema_version") != "smart-objective-intent-v2":
        raise SmartObjectiveIntentError(
            "unsupported smart objective intent schema version"
        )
    if payload.get("objective") != objective:
        raise SmartObjectiveIntentError(
            "semantic intent must preserve the exact raw objective"
        )
    _text_list(payload, "research_questions", require_one=True)
    _text_list(payload, "entities")
    _text_list(payload, "jurisdictions")
    _text_list(payload, "user_constraints")

    temporal = payload.get("temporal")
    if not isinstance(temporal, Mapping):
        raise SmartObjectiveIntentError("semantic intent is missing temporal structure")

    kind = temporal.get("kind")
    if (
        _RELATIVE_PUBLICATION_OR_UPDATE.search(objective)
        and kind != "relative_freshness"
    ):
        raise SmartObjectiveIntentError(
            "explicit relative publication-or-update wording must use relative_freshness"
        )
    if (
        _RELATIVE_PUBLICATION_ONLY.search(objective)
        and kind != "relative_publication_window"
    ):
        raise SmartObjectiveIntentError(
            "explicit relative publication-only wording must use relative_publication_window"
        )
    if temporal.get("uncertainty") != "none" or payload.get("ambiguities"):
        raise SmartObjectiveIntentError(
            "semantic objective intent is ambiguous or unsupported; provide an explicit ResearchSpec"
        )
    quantity = temporal.get("relative_quantity")
    unit = temporal.get("relative_unit")
    freshness_basis = temporal.get("freshness_basis")
    temporal_basis = temporal.get("temporal_basis")
    start = temporal.get("publication_start")
    end = temporal.get("publication_end")
    event_start = temporal.get("event_start")
    event_end = temporal.get("event_end")
    as_of = temporal.get("as_of")

    if kind == "none":
        if temporal_basis != "none" or any(
            value is not None
            for value in (
                quantity,
                unit,
                freshness_basis,
                start,
                end,
                event_start,
                event_end,
                as_of,
            )
        ):
            raise SmartObjectiveIntentError(
                "non-temporal intent must not carry temporal fields"
            )
        return
    if kind in {"relative_freshness", "relative_publication_window", "conjunctive"}:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise SmartObjectiveIntentError(
                "relative temporal intent requires a positive quantity"
            )
        if unit not in {"day", "week"}:
            raise SmartObjectiveIntentError(
                "relative temporal intent requires day or week units"
            )
    if kind == "relative_freshness":
        if (
            temporal_basis != "publication_or_update_within"
            or freshness_basis != "publication_or_update"
            or any(
                value is not None
                for value in (start, end, event_start, event_end, as_of)
            )
        ):
            raise SmartObjectiveIntentError(
                "relative freshness must use publication_or_update_within and no absolute bounds"
            )
        return
    if kind == "relative_publication_window":
        if (
            temporal_basis != "publication_within"
            or freshness_basis != "publication"
            or any(
                value is not None
                for value in (start, end, event_start, event_end, as_of)
            )
        ):
            raise SmartObjectiveIntentError(
                "relative publication windows require publication_within authority and no absolute bounds"
            )
        return
    if kind == "absolute_publication_window":
        if temporal_basis != "publication_within" or any(
            value is not None
            for value in (
                quantity,
                unit,
                freshness_basis,
                event_start,
                event_end,
                as_of,
            )
        ):
            raise SmartObjectiveIntentError(
                "absolute publication windows must carry only publication bounds"
            )
        _validate_absolute_bounds(start, end)
        return
    if kind == "event_window":
        if temporal_basis != "event_within" or any(
            value is not None
            for value in (quantity, unit, freshness_basis, start, end, as_of)
        ):
            raise SmartObjectiveIntentError(
                "event windows must carry only event bounds"
            )
        _validate_absolute_bounds(event_start, event_end, label="event")
        return
    if kind == "current_as_of":
        if temporal_basis != "current_as_of" or any(
            value is not None
            for value in (
                quantity,
                unit,
                freshness_basis,
                start,
                end,
                event_start,
                event_end,
            )
        ):
            raise SmartObjectiveIntentError(
                "current_as_of must carry only an as_of bound"
            )
        _validate_as_of(as_of)
        return
    if kind == "conjunctive":
        if (
            temporal_basis != "conjunctive"
            or freshness_basis != "publication_or_update"
        ):
            raise SmartObjectiveIntentError(
                "conjunctive intent requires publication/update freshness authority"
            )
        if any(value is not None for value in (event_start, event_end, as_of)):
            raise SmartObjectiveIntentError(
                "conjunctive publication/freshness intent cannot carry event/as-of fields"
            )
        _validate_absolute_bounds(start, end)
        return
    raise SmartObjectiveIntentError(f"unsupported temporal intent kind: {kind}")


def materialize_smart_objective_intent(
    payload: Mapping[str, Any],
    *,
    execution_mode: ExecutionMode | str,
    evaluated_at: datetime,
) -> SmartObjectiveMaterialization:
    """Materialize semantic meaning without delegating deterministic authority."""

    objective = str(payload.get("objective") or "")
    validate_smart_objective_intent(payload, objective=objective)
    clock = _clock(evaluated_at)
    mode = (
        execution_mode
        if isinstance(execution_mode, ExecutionMode)
        else ExecutionMode(str(execution_mode))
    )
    base = conservative_research_spec(objective, "general")
    questions = _text_list(payload, "research_questions", require_one=True)
    entities = _text_list(payload, "entities")
    jurisdictions = _text_list(payload, "jurisdictions")
    user_constraints = _text_list(payload, "user_constraints")
    temporal = payload["temporal"]
    kind = str(temporal["kind"])
    evidence_window = base.time_window
    freshness = base.freshness_requirements
    discovery = unbounded_discovery_window()
    temporal_basis = TemporalBasis.NONE

    if kind in {"relative_freshness", "relative_publication_window", "conjunctive"}:
        relative_days = _days(
            int(temporal["relative_quantity"]), str(temporal["relative_unit"])
        )
        relative_start = clock - timedelta(days=relative_days)
    else:
        relative_days = None
        relative_start = None

    if kind == "relative_freshness":
        temporal_basis = TemporalBasis.PUBLICATION_OR_UPDATE_WITHIN
        assert relative_start is not None
        description = f"fresh evidence no older than {relative_days} days"
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                description,
                relative_days,
            ),
        )
        discovery = TimeWindow(
            relative_start.isoformat(),
            clock.isoformat(),
            f"discovery superset for {description}",
            "none",
        )
    elif kind == "relative_publication_window":
        temporal_basis = TemporalBasis.PUBLICATION_WITHIN
        assert relative_start is not None
        description = f"publication within the past {relative_days} days"
        evidence_window = TimeWindow(
            relative_start.isoformat(), clock.isoformat(), description, "none"
        )
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                "Required evidence publication must fall within the relative publication interval.",
                None,
            ),
        )
        discovery = TimeWindow(
            relative_start.isoformat(),
            clock.isoformat(),
            f"discovery superset for {description}",
            "none",
        )
    elif kind == "absolute_publication_window":
        temporal_basis = TemporalBasis.PUBLICATION_WITHIN
        start_raw, end_raw = _validate_absolute_bounds(
            temporal.get("publication_start"), temporal.get("publication_end")
        )
        description = f"publication interval {start_raw} through {end_raw}"
        evidence_window = TimeWindow(start_raw, end_raw, description, "none")
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                "Required evidence publication must fall within the explicit objective interval.",
                None,
            ),
        )
        discovery = TimeWindow(
            start_raw,
            end_raw,
            "provider discovery superset for explicit publication interval",
            "none",
        )
    elif kind == "event_window":
        temporal_basis = TemporalBasis.EVENT_WITHIN
        start_raw, end_raw = _validate_absolute_bounds(
            temporal.get("event_start"), temporal.get("event_end"), label="event"
        )
        evidence_window = TimeWindow(
            start_raw,
            end_raw,
            f"event interval {start_raw} through {end_raw}",
            "none",
        )
        discovery = unbounded_discovery_window()
    elif kind == "current_as_of":
        temporal_basis = TemporalBasis.CURRENT_AS_OF
        as_of = _validate_as_of(temporal.get("as_of"))
        evidence_window = TimeWindow(
            as_of,
            as_of,
            f"state current as of {as_of}",
            "none",
        )
        discovery = unbounded_discovery_window()
    elif kind == "conjunctive":
        temporal_basis = TemporalBasis.CONJUNCTIVE
        assert relative_start is not None
        start_raw, end_raw = _validate_absolute_bounds(
            temporal.get("publication_start"), temporal.get("publication_end")
        )
        publication_description = f"publication interval {start_raw} through {end_raw}"
        evidence_window = TimeWindow(
            start_raw, end_raw, publication_description, "none"
        )
        freshness_description = f"fresh evidence no older than {relative_days} days"
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, freshness_description),
                freshness_description,
                relative_days,
            ),
        )
        publication_start = parse_bound(start_raw)
        discovery_start = min(publication_start, relative_start)
        discovery = TimeWindow(
            discovery_start.isoformat(),
            clock.isoformat(),
            "non-narrowing discovery superset for conjunctive temporal obligations",
            "none",
        )

    spec = replace(
        base,
        execution_mode=mode,
        questions=tuple(
            ResearchQuestion(_semantic_id(base, "question", index, question), question)
            for index, question in enumerate(questions)
        ),
        entities=entities,
        jurisdictions=jurisdictions,
        user_constraints=user_constraints,
        time_window=evidence_window,
        freshness_requirements=freshness,
        ambiguities=tuple(str(item) for item in payload.get("ambiguities", ())),
        assumptions=tuple(str(item) for item in payload.get("assumptions", ())),
        temporal_basis=temporal_basis,
    )
    return SmartObjectiveMaterialization(spec, discovery, dict(payload))


def discovery_window_from_spec(
    spec: ResearchSpec, *, evaluated_at: datetime
) -> TimeWindow:
    """Derive a non-narrowing discovery window for an explicit ResearchSpec."""

    clock = _clock(evaluated_at)
    if spec.temporal_basis in {TemporalBasis.EVENT_WITHIN, TemporalBasis.CURRENT_AS_OF}:
        return unbounded_discovery_window()
    starts: list[datetime] = []
    if spec.time_window.start:
        starts.append(parse_bound(spec.time_window.start))
    ages = [
        item.max_age_days
        for item in spec.freshness_requirements
        if item.max_age_days is not None
    ]
    if ages:
        starts.append(clock - timedelta(days=max(int(value) for value in ages)))
    if not starts:
        return unbounded_discovery_window()
    start = min(starts)
    return TimeWindow(
        start.isoformat(),
        clock.isoformat(),
        "non-narrowing discovery window derived from explicit ResearchSpec",
        "none",
    )


def degraded_intent_fixture(
    objective: str,
    *,
    execution_mode: ExecutionMode | str,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Express the narrow deterministic grammar as the same versioned contract."""

    spec = materialize_smart_fallback_spec(
        objective,
        execution_mode=execution_mode,
        evaluated_at=evaluated_at,
    )
    ages = [
        item.max_age_days
        for item in spec.freshness_requirements
        if item.max_age_days is not None
    ]
    if ages:
        temporal = {
            "kind": "relative_freshness",
            "relative_quantity": max(ages),
            "relative_unit": "day",
            "freshness_basis": "publication_or_update",
            "temporal_basis": "publication_or_update_within",
            "publication_start": None,
            "publication_end": None,
            "event_start": None,
            "event_end": None,
            "as_of": None,
            "uncertainty": "none",
            "rationale": "narrow deterministic degraded fallback",
        }
    elif spec.time_window.start or spec.time_window.end:
        temporal = {
            "kind": "absolute_publication_window",
            "relative_quantity": None,
            "relative_unit": None,
            "freshness_basis": None,
            "temporal_basis": "publication_within",
            "publication_start": spec.time_window.start,
            "publication_end": spec.time_window.end,
            "event_start": None,
            "event_end": None,
            "as_of": None,
            "uncertainty": "none",
            "rationale": "narrow deterministic degraded fallback",
        }
    else:
        temporal = {
            "kind": "none",
            "relative_quantity": None,
            "relative_unit": None,
            "freshness_basis": None,
            "temporal_basis": "none",
            "publication_start": None,
            "publication_end": None,
            "event_start": None,
            "event_end": None,
            "as_of": None,
            "uncertainty": "none",
            "rationale": "objective contains no deterministic temporal signal",
        }
    return {
        "schema_version": "smart-objective-intent-v2",
        "objective": objective,
        "research_questions": [objective],
        "entities": [],
        "jurisdictions": [],
        "user_constraints": [],
        "temporal": temporal,
        "assumptions": [
            "semantic interpreter unavailable; deterministic degraded fallback used"
        ],
        "ambiguities": [],
    }


def interpret_smart_objective(
    *,
    semantic_service: Any,
    status: Any,
    objective: str,
    invocation_id: str,
    evaluated_at: datetime,
) -> Any:
    """Persist one strict semantic interpretation of the raw user objective."""

    mode = str(getattr(status.execution_mode, "value", status.execution_mode))
    if mode == "deterministic_debug":
        fixture = degraded_intent_fixture(
            objective,
            execution_mode=mode,
            evaluated_at=evaluated_at,
        )
    else:
        fixture = {
            "schema_version": "smart-objective-intent-v2",
            "objective": objective,
            "research_questions": [objective],
            "entities": [],
            "jurisdictions": [],
            "user_constraints": [],
            "temporal": {
                "kind": "none",
                "relative_quantity": None,
                "relative_unit": None,
                "freshness_basis": None,
                "temporal_basis": "none",
                "publication_start": None,
                "publication_end": None,
                "event_start": None,
                "event_end": None,
                "as_of": None,
                "uncertainty": "none",
                "rationale": "unused fixture outside deterministic_debug",
            },
            "assumptions": [],
            "ambiguities": [],
        }

    def post_validate(payload: dict[str, Any]) -> None:
        validate_smart_objective_intent(payload, objective=objective)

    return call_authorized_structured(
        semantic_service=semantic_service,
        semantic_context={
            "run_id": str(status.id),
            "run_revision": status.lifecycle_revision,
            "stage": "smart_objective_intent",
            "schema_name": "smart-objective-intent-v2",
            "schema_version": 2,
            "artifact_type": "smart_objective_intent",
            "idempotency_key": f"smart:objective-intent:{status.id}:r1",
            "invocation_id": invocation_id,
        },
        deterministic_fixture=fixture,
        actor_identifier="fsearch_smart_objective_interpreter",
        host_artifact_supplier=getattr(
            semantic_service, "host_artifact_supplier", None
        ),
        provider="local",
        model=None,
        schema=SMART_OBJECTIVE_INTENT_SCHEMA,
        system_prompt=(
            "Interpret the raw research objective into the strict schema without answering it. "
            "Preserve objective exactly. Decompose the objective into explicit research_questions, "
            "named entities, jurisdictions, and user_constraints without inventing information. "
            "These semantic fields become deterministic ResearchSpec inputs and downstream search "
            "planning context; do not emit IDs or provider parameters. Classify the evidentiary temporal "
            "dimension explicitly. 'Published in/within the past/last N days' must use "
            "kind=relative_publication_window, freshness_basis=publication, and "
            "temporal_basis=publication_within; excluding updates does not make that wording ambiguous. "
            "'Updated/current within the past N days' is publication_or_update_within. 'Publication or update within the "
            "last N days' is one relative_freshness obligation, never conjunctive. Conjunctive requires "
            "two independent obligations, for example 'published between <date1> and <date2> and updated "
            "within the last N days'. An event that occurred during an "
            "explicit interval is event_within and must use event_start/event_end, not publication fields. "
            "'Status/state as of <date>' is current_as_of and must use as_of without inventing publication "
            "semantics. Use conjunctive only when publication-window and freshness obligations are both "
            "explicit. Ambiguous wording must remain ambiguous rather than selecting a narrower basis. "
            "Never emit provider qdr/tbs parameters, never compute dates from the current clock, and never "
            "invent missing dates. Always emit all temporal fields: kind, relative_quantity, relative_unit, "
            "freshness_basis, temporal_basis, publication_start, publication_end, event_start, event_end, "
            "as_of, uncertainty, and rationale. Explicit negations such as 'no publication-date restriction' "
            "are non-temporal intent: use temporal.kind=none, temporal_basis=none, set every bound/freshness "
            "field to null, set uncertainty to none when unambiguous, and provide a rationale. "
            "For every other temporal kind, populate only the fields authorized by that kind and set "
            "forbidden temporal fields to null. Put unresolved ambiguity in ambiguities and mark uncertainty "
            "ambiguous or unsupported."
        ),
        user_prompt=json.dumps({"objective": objective}, ensure_ascii=False),
        prompt_version=SMART_OBJECTIVE_INTENT_PROMPT_VERSION,
        post_validate=post_validate,
    )


__all__ = [
    "SMART_OBJECTIVE_INTENT_SCHEMA",
    "SmartObjectiveIntentError",
    "SmartObjectiveMaterialization",
    "degraded_intent_fixture",
    "discovery_window_from_spec",
    "interpret_smart_objective",
    "materialize_smart_objective_intent",
    "unbounded_discovery_window",
    "validate_smart_objective_intent",
]
