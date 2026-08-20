"""Temporary compatibility facade for legacy research-store service imports.

Issue #264 removes the former generic implementation aggregation from this
module.  Corpus ownership remains in ``corpus_service``; claim/evidence
manifest and audit ownership now live in ``research_store.assessment``.
Existing imports remain identity-preserving until #269 removes campaign
compatibility facades.
"""

from __future__ import annotations

import json

from .assessment.audit import (
    AuditService,
    compute_audit_identity_hash,
    resolve_model_fingerprint,
)
from .assessment.claims import ClaimManifestService
from .corpus_service import CorpusService, ParsedContent, PreparedIngest
from .domain import IngestRequest, IngestResult

__all__ = [
    "AuditService",
    "ClaimManifestService",
    "CorpusService",
    "IngestRequest",
    "IngestResult",
    "ParsedContent",
    "PreparedIngest",
    "compute_audit_identity_hash",
    "dumps",
    "json_default",
    "resolve_model_fingerprint",
]


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dumps(value) -> str:
    return json.dumps(value, indent=2, default=json_default)
