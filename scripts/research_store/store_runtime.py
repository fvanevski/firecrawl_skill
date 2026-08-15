"""Shared research-store runtime factories outside entrypoint concerns."""

from __future__ import annotations

from functools import partial

from .postgres import PostgresUnitOfWork, connect


def database(config):
    config.require_database()
    return connect(config.database_url)


def uow_factory(config):
    return partial(
        PostgresUnitOfWork,
        config.database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
    )
