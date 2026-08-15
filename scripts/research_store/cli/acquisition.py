from __future__ import annotations

import json
import sys
from uuid import UUID

COMMANDS = {
    "budget-record",
    "search-plan-record",
    "search-plan-get",
    "search-plan-query-get",
    "search-response-record",
    "search-response-get",
    "search-response-replay",
    "candidate-record-response",
    "candidate-get",
    "candidate-list",
    "candidate-occurrences-list",
    "candidate-assign-group",
    "acquisition-search",
    "acquisition-reconcile",
    "candidate-list-paginated",
    "candidate-card",
    "candidate-triage-input",
    "candidate-replay",
}


def run(args, config, deps) -> int:
    command = args.command
    if command == "budget-record":
        from ..acquisition_admin import record_budget

        print(deps.dumps(record_budget(config, args, deps)))
        return 0
    if command == "search-plan-record":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        with open(args.search_plan, "r", encoding="utf-8") as file:
            plan_payload = json.load(file)
        plan_id = run_svc.record_search_plan(
            status.id,
            UUID(args.research_spec_id),
            args.revision,
            plan_payload,
            args.idempotency_key,
        )
        print(deps.dumps({"id": plan_id, "external_run_id": args.external_id}))
        return 0
    if command == "search-plan-get":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        plan = run_svc.get_search_plan(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            revision=args.revision,
        )
        print(deps.dumps(plan))
        return 0
    if command == "search-plan-query-get":
        run_svc = deps.build_run_service(config)
        print(deps.dumps(run_svc.get_plan_query(UUID(args.query_id))))
        return 0
    if command == "search-response-record":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        if args.payload_file:
            with open(args.payload_file, "rb") as file:
                raw_payload = file.read()
        else:
            raw_payload = sys.stdin.buffer.read()
        resp = run_svc.record_search_response(
            status.id,
            args.query_text,
            args.backend,
            raw_payload,
            args.idempotency_key,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            provider_request_id=args.provider_request_id,
            parser_version=args.parser_version,
            http_status=args.http_status,
        )
        print(deps.dumps(resp))
        return 0
    if command == "search-response-get":
        print(
            deps.dumps(
                deps.build_run_service(config).get_search_response(UUID(args.response_id))
            )
        )
        return 0
    if command == "search-response-replay":
        replay = deps.build_run_service(config).replay_search_response(
            UUID(args.response_id)
        )
        out = {
            "id": str(replay.id),
            "run_id": str(replay.run_id),
            "query_text": replay.query_text,
            "backend": replay.backend,
            "status": replay.status,
            "parser_version": replay.parser_version,
            "raw_blob_sha256": replay.raw_blob_sha256,
            "content_sha256": replay.content_sha256,
            "raw_bytes_len": len(replay.raw_bytes),
            "integrity_verified": replay.verify_integrity(),
            "result_count": replay.result_count,
            "parsed_json": replay.parsed_json,
        }
        print(deps.dumps(out))
        return 0
    if command == "candidate-record-response":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        print(
            deps.dumps(
                run_svc.record_response_candidates(
                    status.id, UUID(args.search_response_id)
                )
            )
        )
        return 0
    if command == "candidate-get":
        print(
            deps.dumps(
                deps.build_run_service(config).get_candidate(UUID(args.candidate_id))
            )
        )
        return 0
    if command == "candidate-list":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        cands = run_svc.list_candidates(
            status.id,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=(
                UUID(args.duplicate_group_id) if args.duplicate_group_id else None
            ),
        )
        print(deps.dumps(cands))
        return 0
    if command == "candidate-occurrences-list":
        print(
            deps.dumps(
                deps.build_run_service(config).list_candidate_occurrences(
                    UUID(args.candidate_id)
                )
            )
        )
        return 0
    if command == "candidate-assign-group":
        run_svc = deps.build_run_service(config)
        group_id = UUID(args.group_id) if args.group_id else None
        resolved = run_svc.assign_duplicate_group(
            [UUID(candidate_id) for candidate_id in args.candidate_ids],
            group_id=group_id,
        )
        print(deps.dumps({"duplicate_group_id": resolved}))
        return 0
    if command == "acquisition-search":
        from ..acquisition_authority import (
            AcquisitionPreflightError,
            require_authoritative_acquisition,
        )
        from ..acquisition_service import AcquisitionIdempotencyConflictError
        from ..container import build_acquisition_service

        try:
            config.require_database()
            run_svc = deps.build_run_service(config)
            status = run_svc.status(external_id=args.external_id)
            context = require_authoritative_acquisition(
                run_id=status.id, config=config
            )
            acq_svc = build_acquisition_service(config)
        except (AcquisitionPreflightError, KeyError, RuntimeError, ValueError) as exc:
            print(
                deps.dumps(
                    {
                        "schema_version": "authoritative-acquisition-error-v1",
                        "status": "failed",
                        "failure_stage": "preflight",
                        "error": str(exc)[:500],
                    }
                ),
                file=sys.stderr,
            )
            return 2
        try:
            result = acq_svc.execute_search(
                status.id,
                args.query_text,
                backend=args.backend,
                plan_id=UUID(args.plan_id) if args.plan_id else None,
                plan_query_id=(
                    UUID(args.plan_query_id) if args.plan_query_id else None
                ),
                idempotency_key=args.idempotency_key,
                limit=args.limit,
                sources=args.sources,
                tbs=args.tbs,
                authority_context=context,
                replay_existing=True,
            )
        except AcquisitionIdempotencyConflictError as exc:
            print(
                deps.dumps(
                    {
                        "schema_version": "authoritative-acquisition-error-v1",
                        "status": "failed",
                        "failure_stage": "idempotency",
                        "error": str(exc)[:500],
                    }
                ),
                file=sys.stderr,
            )
            return 3
        print(
            deps.dumps(
                {
                    "search_response_id": str(result.search_response_id),
                    "run_id": str(result.run_id),
                    "query_text": result.query_text,
                    "backend": result.backend,
                    "status": result.status,
                    "candidate_count": result.candidate_count,
                    "postgres_committed": result.postgres_committed,
                    "event_id": str(result.event_id) if result.event_id else None,
                    "replayed": result.replayed,
                }
            )
        )
        return 0 if result.status in {"succeeded", "empty"} else 1
    if command == "acquisition-reconcile":
        from ..container import build_acquisition_service

        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        reconciled = build_acquisition_service(config).reconcile_pending_searches(status.id)
        print(deps.dumps(reconciled))
        return 0
    if command == "candidate-list-paginated":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        paginated = run_svc.list_candidates_paginated(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            query_text=args.query_text,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=(
                UUID(args.duplicate_group_id) if args.duplicate_group_id else None
            ),
            limit=args.limit,
            offset=args.offset,
        )
        print(deps.dumps(paginated))
        return 0
    if command == "candidate-card":
        card = deps.build_run_service(config).get_candidate_card(
            UUID(args.candidate_id), max_snippet_length=args.max_snippet_length
        )
        print(deps.dumps(card))
        return 0
    if command == "candidate-triage-input":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        triage = run_svc.build_triage_input(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            query_text=args.query_text,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=(
                UUID(args.duplicate_group_id) if args.duplicate_group_id else None
            ),
            limit=args.limit,
            offset=args.offset,
            max_snippet_length=args.max_snippet_length,
        )
        print(deps.dumps(triage))
        return 0
    if command == "candidate-replay":
        run_svc = deps.build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        replayed = run_svc.replay_candidates(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            limit=args.limit,
            offset=args.offset,
        )
        print(deps.dumps(replayed))
        return 0
    raise AssertionError(command)
