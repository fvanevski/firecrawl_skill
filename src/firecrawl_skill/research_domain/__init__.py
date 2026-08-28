"""Versioned research workflow domain contracts."""

from .codec import DomainValidationError, dumps
from .models import HandoffPayload
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
    "MODEL_BY_VERSION",
    "DomainValidationError",
    "HandoffPayload",
    "ValidationContext",
    "dumps",
    "load_model",
    "schema_registry",
    "serialize_model",
    "validate_references",
]
