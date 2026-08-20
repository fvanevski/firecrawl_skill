"""Temporary compatibility facade for deterministic audit-packet identity."""

from .assessment.audit_packet import compute_audit_packet_hash_from_db

__all__ = ["compute_audit_packet_hash_from_db"]
