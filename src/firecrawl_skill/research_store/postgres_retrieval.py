"""Compatibility facade for canonical retrieval/projection PostgreSQL repositories."""

from .retrieval.postgres import PostgresRetrievalRepository
from .retrieval.projection.postgres_jobs import PostgresIndexJobRepository

__all__ = ["PostgresIndexJobRepository", "PostgresRetrievalRepository"]
