from __future__ import annotations

from uuid import UUID

from .. import retrieval_admin

COMMANDS = {
    "corpus-overview",
    "search-assets",
    "inspect-asset",
    "fetch-passages",
    "expand-relationships",
    "build-evidence-packet",
}


def run(args, config, deps):
    service = deps.build_service(config)
    if args.command == "corpus-overview":
        service.corpus_overview()
        return None
    if args.command == "search-assets":
        filters = {
            key: value
            for key, value in {
                "domain": args.domain,
                "source_type": args.source_type,
                "date_from": args.date_from,
                "date_to": args.date_to,
            }.items()
            if value
        }
        execution, candidates = service.search_assets(
            args.query,
            filters=filters,
            candidate_limit=args.limit,
            run_id=deps._resolve_run_id(config, args.research_run_id),
            requested_mode=getattr(args, "mode", "hybrid"),
        )
        _result = {
            "execution": {
                "requested_mode": execution.requested_mode,
                "executed_mode": execution.executed_mode,
                "mechanical_status": execution.mechanical_status.value
                if hasattr(execution.mechanical_status, "value")
                else execution.mechanical_status,
                "component_health": execution.component_health,
                "errors": execution.errors,
                "warnings": execution.warnings,
                "stage_counts": execution.stage_counts,
                "index_fingerprint": execution.index_fingerprint,
                "skipped_stages": execution.skipped_stages,
                "timing": execution.timing,
            },
            "candidates": candidates,
        }
        return None
    if args.command == "inspect-asset":
        service.inspect_asset(UUID(args.id))
        return None
    if args.command == "fetch-passages":
        retrieval_admin.fetch_passages(
            service,
            config,
            args,
            resolve_run_id=deps._resolve_run_id,
            uow_factory=deps._uow_factory,
        )
        return None
    if args.command == "expand-relationships":
        service.expand_relationships(
            [UUID(value) for value in args.ids],
            max_hops=args.max_hops,
            max_results=args.max_results,
            max_tokens=args.max_tokens,
        )
        return None
    if args.command == "build-evidence-packet":
        service.build_evidence_packet(
            [UUID(value) for value in args.ids], max_tokens=args.max_tokens
        )
        return None
    raise AssertionError(args.command)
