"""Retired issue-300 temporal PostgreSQL compatibility module.

Temporal candidate normalization and run-locked EvidencePacket persistence are
owned exclusively by the canonical repository context in ``postgres_uow_core``.
This module intentionally exposes no alternate UoW or repository implementation;
it remains only as a temporary import tombstone for the issue-300 branch.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
