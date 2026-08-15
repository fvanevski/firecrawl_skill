"""Application helpers for evidence-oriented administrative commands."""

from __future__ import annotations


def export_invocation(config, invocation_id, *, uow_factory) -> dict:
    with uow_factory(config)() as uow:
        return uow.export_invocation(invocation_id)
