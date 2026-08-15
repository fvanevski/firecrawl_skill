"""Application assembly for host-agent handoff payloads."""

from __future__ import annotations

from functools import partial
from uuid import UUID

from .handoff import HandoffBuilder
from .postgres import PostgresUnitOfWork


def build_handoff(config, args) -> dict:
    config.require_database()
    token_limits = {}
    if args.token_limit_max_input is not None:
        token_limits["max_input_tokens"] = args.token_limit_max_input
    if args.token_limit_max_output is not None:
        token_limits["max_output_tokens"] = args.token_limit_max_output
    if args.token_limit_max_retrieval is not None:
        token_limits["max_retrieval_candidates"] = args.token_limit_max_retrieval
    factory = partial(
        PostgresUnitOfWork,
        config.database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
        config.chunker_name,
    )
    return HandoffBuilder(
        factory,
        token_limits=token_limits if token_limits else None,
        max_passages=args.max_passages,
        max_claims=args.max_claims,
    ).build(UUID(args.run_id)).to_dict()
