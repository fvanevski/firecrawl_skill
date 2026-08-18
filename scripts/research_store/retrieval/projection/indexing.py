"""Projection namespace facade for the baseline-stable indexing implementation."""

from ...indexing import (
    IndexWorker as IndexWorker,
    LeaseLost as LeaseLost,
    OpenAICompatibleEmbedder as OpenAICompatibleEmbedder,
)

__all__ = ["IndexWorker", "LeaseLost", "OpenAICompatibleEmbedder"]
