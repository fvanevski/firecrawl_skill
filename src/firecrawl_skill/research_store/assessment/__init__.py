"""Canonical assessment vertical slice.

Issue #264 groups deterministic assessment and PostgreSQL-backed assessment
services behind one discoverable package boundary. Baseline-tracked flat
modules remain temporary bridges until their existing type debt is resolved;
debt-free assessment implementations are owned directly here, with legacy
flat import paths retained only as #269 compatibility facades.
"""

from .audit import (
    AUDIT_IDENTITY_VERSION,
    AUDIT_MODEL_IMPLEMENTATION_VERSION,
    AuditService,
    compute_audit_identity_hash,
    resolve_model_fingerprint,
)
from .audit_packet import compute_audit_packet_hash_from_db
from .claims import ClaimManifestService

__all__ = [
    "AUDIT_IDENTITY_VERSION",
    "AUDIT_MODEL_IMPLEMENTATION_VERSION",
    "AuditService",
    "ClaimManifestService",
    "compute_audit_identity_hash",
    "compute_audit_packet_hash_from_db",
    "resolve_model_fingerprint",
]
