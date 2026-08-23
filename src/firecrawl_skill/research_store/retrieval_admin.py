"""Application helpers for retrieval-oriented CLI commands."""

from __future__ import annotations

import json
from uuid import UUID

from .identity_resolver import resolve_corpus_identity


def fetch_passages(service, config, args, *, resolve_run_id, uow_factory):
    chunk_ids = [UUID(value) for value in args.ids]
    factory = uow_factory(config)
    with factory() as uow:
        for identifier in chunk_ids:
            resolution = resolve_corpus_identity(uow.connection, identifier)
            if resolution.identity_type != "chunk":
                raise ValueError(
                    json.dumps(
                        {
                            "code": "wrong_identity_type",
                            "command": "research-db fetch-passages",
                            "provided_id": str(identifier),
                            "expected_identity_type": "chunk",
                            "detected_identity_type": resolution.identity_type,
                            "identity": resolution.to_dict(),
                            "guidance": (
                                "pass PostgreSQL chunks.id values; use finspect passages "
                                "for promotion subjects, search candidates, extraction "
                                "attempts, sources, snapshots, documents, or derivations"
                            ),
                        },
                        sort_keys=True,
                    )
                )
    result = service.fetch_passages(
        chunk_ids,
        max_tokens=args.max_tokens,
        max_passages=args.max_passages,
    )
    run_id = resolve_run_id(config, args.research_run_id)
    if run_id:
        with factory() as uow:
            for rank, passage in enumerate(result, 1):
                uow.retrieval_events.log_retrieval(
                    run_id,
                    {
                        "stage": "passage_fetch",
                        "retriever": "explicit_selection",
                        "candidate_type": "chunk",
                        "candidate_id": passage["chunk_id"],
                        "rank": rank,
                        "selected": True,
                    },
                )
    return result
