"""Canonical assessment vertical slice.

Issue #264 groups deterministic assessment and PostgreSQL-backed assessment
services behind one discoverable package boundary.  Several long-lived flat
modules remain temporary compatibility/debt bridges until the campaign cleanup
issue owns their physical relocation; the canonical claim and audit services
live here directly.
"""

from .audit import (
    AUDIT_IDENTITY_VERSION,
    AUDIT_MODEL_IMPLEMENTATION_VERSION,
    AuditService,
    compute_audit_identity_hash,
    resolve_model_fingerprint,
)
from .claims import ClaimManifestService

__all__ = [
    "AUDIT_IDENTITY_VERSION",
    "AUDIT_MODEL_IMPLEMENTATION_VERSION",
    "AuditService",
    "ClaimManifestService",
    "compute_audit_identity_hash",
    "resolve_model_fingerprint",
]
