"""Temporary canonical facade for the existing ``research_store`` package.

The implementation remains under ``scripts/research_store`` for Phase 1.  This
facade intentionally aliases that package rather than re-exporting copied symbols,
so import-time wiring and mutable module state have one identity.
"""

from .._compat import expose_legacy_package as _expose_legacy_package

_expose_legacy_package(__name__, "research_store")
