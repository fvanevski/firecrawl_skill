"""Projection namespace facade for the baseline-stable indexing implementation."""

from ...indexing import IndexWorker, LeaseLost, OpenAICompatibleEmbedder

__all__ = ["IndexWorker", "LeaseLost", "OpenAICompatibleEmbedder"]
