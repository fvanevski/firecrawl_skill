"""Application helpers for retrieval-oriented CLI commands."""

from __future__ import annotations

from uuid import UUID


def fetch_passages(service, config, args, *, resolve_run_id, uow_factory):
    result = service.fetch_passages(
        [UUID(value) for value in args.ids],
        max_tokens=args.max_tokens,
        max_passages=args.max_passages,
    )
    run_id = resolve_run_id(config, args.research_run_id)
    if run_id:
        with uow_factory(config)() as uow:
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
