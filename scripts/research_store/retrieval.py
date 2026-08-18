"""Compatibility facade for the canonical retrieval capability package."""

from .retrieval import (
    CohereCompatibleReranker,
    pack_context,
    reciprocal_rank_fusion,
    validate_relation,
)

__all__ = [
    "CohereCompatibleReranker",
    "pack_context",
    "reciprocal_rank_fusion",
    "validate_relation",
]
