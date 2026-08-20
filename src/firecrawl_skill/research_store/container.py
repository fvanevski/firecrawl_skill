"""Temporary compatibility facade for the canonical composition root.

New production wiring belongs in :mod:`research_store.composition`.  This
module preserves the historical builder import surface while Phase 5 migrates
callers; it owns no construction or policy logic.
"""

from .composition import (
    build_acquisition_service,
    build_audit_service,
    build_claim_service,
    build_evidence_service,
    build_extraction_service,
    build_invocation_service,
    build_orchestrator,
    build_resource_governor,
    build_run_service,
    build_semantic_service,
    build_service,
    build_strategy_service,
    build_uow_factory,
    build_workflow_operation_service,
)

__all__ = [
    "build_acquisition_service",
    "build_audit_service",
    "build_claim_service",
    "build_evidence_service",
    "build_extraction_service",
    "build_invocation_service",
    "build_orchestrator",
    "build_resource_governor",
    "build_run_service",
    "build_semantic_service",
    "build_service",
    "build_strategy_service",
    "build_uow_factory",
    "build_workflow_operation_service",
]
