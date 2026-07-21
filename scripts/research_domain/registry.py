"""Schema-version registry and compatibility policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .codec import DomainValidationError, dumps, from_dict, schema_for, to_dict, write_schema
from .models import CANONICAL_MODELS
from .validation import ValidationContext, validate_references


MODEL_BY_VERSION = {model.SCHEMA_VERSION: model for model in CANONICAL_MODELS}
CURRENT_VERSION_BY_MODEL = {model.__name__: model.SCHEMA_VERSION for model in CANONICAL_MODELS}
COMPATIBILITY_POLICY = {
    version: {
        "current": True,
        "readable_versions": (version,),
        "write_version": version,
        "predecessors": (),
    }
    for version in MODEL_BY_VERSION
}


def load_model(payload: dict, context: ValidationContext | None = None):
    if not isinstance(payload, dict):
        raise DomainValidationError("domain payload must be an object")
    version = payload.get("schema_version")
    model_type = MODEL_BY_VERSION.get(version)
    if model_type is None:
        raise DomainValidationError(f"unsupported schema_version: {version!r}")
    model = from_dict(model_type, payload)
    return validate_references(model, context) if context is not None else model


def serialize_model(model) -> dict:
    version = getattr(model, "schema_version", None)
    if version not in MODEL_BY_VERSION or not isinstance(model, MODEL_BY_VERSION[version]):
        raise DomainValidationError("model is not registered for its schema_version")
    return to_dict(model)


def schema_registry() -> dict:
    return {version: schema_for(model) for version, model in sorted(MODEL_BY_VERSION.items())}


def write_schemas(output_dir: Path) -> None:
    for version, model in sorted(MODEL_BY_VERSION.items()):
        write_schema(model, output_dir / f"{version}.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", type=Path)
    parser.add_argument("--print-registry", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        write_schemas(args.write)
    if args.print_registry:
        print(json.dumps({key: dict(value) for key, value in COMPATIBILITY_POLICY.items()}, indent=2, default=list, sort_keys=True))
    if not args.write and not args.print_registry:
        parser.error("one of --write or --print-registry is required")
    return 0


__all__ = [
    "COMPATIBILITY_POLICY",
    "CURRENT_VERSION_BY_MODEL",
    "MODEL_BY_VERSION",
    "dumps",
    "load_model",
    "schema_registry",
    "serialize_model",
    "write_schemas",
]


if __name__ == "__main__":
    raise SystemExit(main())
