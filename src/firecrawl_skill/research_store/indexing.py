"""Compatibility import for canonical retrieval projection indexing."""
from .retrieval.projection.indexing import IndexWorker, LeaseLost, OpenAICompatibleEmbedder

__all__ = ["IndexWorker", "LeaseLost", "OpenAICompatibleEmbedder"]
