"""Versioned research workflow domain contracts."""

from .codec import DomainValidationError, dumps
from .models import *  # noqa: F403 - package intentionally exports the contracts.
from .registry import (
    COMPATIBILITY_POLICY,
    CURRENT_VERSION_BY_MODEL,
    MODEL_BY_VERSION,
    load_model,
    schema_registry,
    serialize_model,
)
from .validation import ValidationContext, validate_references

__all__ = [
    "COMPATIBILITY_POLICY",
    "CURRENT_VERSION_BY_MODEL",
    "DomainValidationError",
    "MODEL_BY_VERSION",
    "ValidationContext",
    "dumps",
    "load_model",
    "schema_registry",
    "serialize_model",
    "validate_references",
]
