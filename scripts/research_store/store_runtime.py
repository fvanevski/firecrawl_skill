"""Shared research-store runtime helpers outside entrypoint concerns."""

from __future__ import annotations

from .composition import build_uow_factory as uow_factory
from .postgres import connect


def database(config):
    config.require_database()
    return connect(config.database_url)


__all__ = ["database", "uow_factory"]
