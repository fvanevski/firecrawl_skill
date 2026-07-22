"""Strict deterministic codecs and JSON-Schema generation."""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import types
from typing import Any, get_args, get_origin, get_type_hints
from uuid import UUID


class DomainValidationError(ValueError):
    """A schema, type, or referential contract was rejected."""


def from_dict(model_type, payload: dict):
    try:
        return _decode(model_type, payload, "$")
    except DomainValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(str(exc)) from exc


def _decode(annotation, value, path):
    if annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType,):
        if value is None and type(None) in args:
            return None
        errors = []
        for option in args:
            if option is type(None):
                continue
            try:
                return _decode(option, value, path)
            except DomainValidationError as exc:
                errors.append(str(exc))
        raise DomainValidationError(f"{path}: value matches no allowed type: {errors}")
    if origin is tuple:
        if not isinstance(value, list):
            raise DomainValidationError(f"{path}: expected array")
        item_type = args[0] if args else Any
        return tuple(
            _decode(item_type, item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if origin is dict:
        if not isinstance(value, dict):
            raise DomainValidationError(f"{path}: expected object")
        key_type, value_type = args or (str, Any)
        return {
            _decode(key_type, key, f"{path}.<key>"): _decode(
                value_type, item, f"{path}.{key}"
            )
            for key, item in value.items()
        }
    if is_dataclass(annotation):
        if not isinstance(value, dict):
            raise DomainValidationError(f"{path}: expected object")
        model_fields = {item.name: item for item in fields(annotation)}
        unknown = sorted(set(value) - set(model_fields))
        if unknown:
            raise DomainValidationError(f"{path}: unexpected fields: {unknown}")
        missing = [
            name
            for name, item in model_fields.items()
            if name not in value
            and item.default is MISSING
            and item.default_factory is MISSING
        ]
        if missing:
            raise DomainValidationError(f"{path}: missing required fields: {missing}")
        hints = get_type_hints(annotation)
        decoded = {
            name: _decode(hints[name], value[name], f"{path}.{name}") for name in value
        }
        try:
            return annotation(**decoded)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(f"{path}: {exc}") from exc
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                f"{path}: unsupported enum value {value!r}"
            ) from exc
    if annotation is UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise DomainValidationError(f"{path}: expected UUID") from exc
    if annotation is datetime:
        if not isinstance(value, str):
            raise DomainValidationError(f"{path}: expected datetime string")
        try:
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError) as exc:
            raise DomainValidationError(f"{path}: invalid datetime: {value}") from exc
    if annotation is str:
        if not isinstance(value, str):
            raise DomainValidationError(f"{path}: expected string")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise DomainValidationError(f"{path}: expected boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainValidationError(f"{path}: expected integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DomainValidationError(f"{path}: expected number")
        return float(value)
    raise DomainValidationError(f"{path}: unsupported annotation {annotation}")


def to_dict(value):
    if is_dataclass(value):
        return {item.name: to_dict(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [to_dict(item) for item in value]
    if isinstance(value, list):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): to_dict(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def dumps(value) -> str:
    return json.dumps(
        to_dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def schema_for(model_type) -> dict:
    definitions = {}
    constraints = {
        ("ResearchSpec", "objective"): {"minLength": 1},
        ("ResearchSpec", "research_archetype"): {"minLength": 1},
        ("ResearchSpec", "questions"): {"minItems": 1},
        ("ResearchSpec", "completion_criteria"): {"minItems": 1},
        ("SearchPlan", "revision"): {"minimum": 1},
        ("SearchPlan", "queries"): {"minItems": 1},
        ("SearchQuery", "query"): {"minLength": 1},
        ("SearchQuery", "facet"): {"minLength": 1},
        ("SearchQuery", "expected_contribution"): {"minLength": 1},
        ("SearchQuery", "priority"): {"minimum": 0},
        ("CandidateAssessment", "priority"): {"minimum": 0, "maximum": 100},
        ("CandidateAssessment", "rationale"): {"minLength": 1},
        ("CandidateAssessment", "confidence"): {"minimum": 0, "maximum": 1},
        ("CoverageLedger", "revision"): {"minimum": 1},
        ("CoverageItem", "confidence"): {"minimum": 0, "maximum": 1},
        ("StrategyRevisionProposal", "run_revision"): {"minimum": 1},
        ("StrategyRevisionProposal", "coverage_revision"): {"minimum": 1},
        ("StrategyRevisionProposal", "target_coverage_item_ids"): {"minItems": 1},
        ("StrategyRevisionProposal", "expected_contribution"): {"minLength": 1},
        ("StrategyRevisionProposal", "rationale"): {"minLength": 1},
        ("StrategyRevisionProposal", "confidence"): {"minimum": 0, "maximum": 1},
        ("ClaimEvidenceBinding", "passage_ids"): {"minItems": 1},
        ("ClaimEvidenceBinding", "confidence"): {"minimum": 0, "maximum": 1},
        ("StructuredDataRequirement", "required_fields"): {"minItems": 1},
    }

    def build(annotation, *, root=False):
        if annotation is Any:
            return {}
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in (types.UnionType,):
            return {
                "anyOf": [
                    build(option) if option is not type(None) else {"type": "null"}
                    for option in args
                ]
            }
        if origin is tuple:
            return {"type": "array", "items": build(args[0] if args else Any)}
        if origin is dict:
            return {
                "type": "object",
                "additionalProperties": build(args[1] if args else Any),
            }
        if is_dataclass(annotation):
            name = annotation.__name__
            if not root:
                if name not in definitions:
                    definitions[name] = {}
                    definitions[name] = object_schema(annotation)
                return {"$ref": f"#/$defs/{name}"}
            return object_schema(annotation)
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return {"type": "string", "enum": [item.value for item in annotation]}
        if annotation is UUID:
            return {"type": "string", "format": "uuid"}
        if annotation is datetime:
            return {"type": "string", "format": "date-time"}
        if annotation is str:
            return {"type": "string"}
        if annotation is bool:
            return {"type": "boolean"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        raise TypeError(f"unsupported schema annotation: {annotation}")

    def object_schema(annotation):
        hints = get_type_hints(annotation)
        properties = {
            item.name: {
                **build(hints[item.name]),
                **constraints.get((annotation.__name__, item.name), {}),
            }
            for item in fields(annotation)
        }
        version = getattr(annotation, "SCHEMA_VERSION", None)
        if version and "schema_version" in properties:
            properties["schema_version"] = {"type": "string", "const": version}
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": [item.name for item in fields(annotation)],
        }

    root = build(model_type, root=True)
    version = model_type.SCHEMA_VERSION
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://fvanevski.github.io/firecrawl_skill/schemas/{version}.json",
        "title": model_type.__name__,
        **root,
        "$defs": dict(sorted(definitions.items())),
    }


def write_schema(model_type, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(schema_for(model_type), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
