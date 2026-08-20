from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from uuid import UUID

COMMANDS = {
    "run-start",
    "run-status",
    "run-operation-start",
    "run-operation-finish",
    "run-mode-change",
    "run-transition",
    "run-finish",
    "run-reopen",
    "run-cancel",
    "run-annotate",
    "run-verify",
    "run-audit",
    "run-compare",
}


def run(args, config, deps) -> int:
    command = args.command
    if command == "run-start":
        status = deps.build_run_service(config).create(
            args.objective,
            args.external_id,
            execution_mode=args.mode,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            skill_version="research-store-v4",
        )
        print(deps.dumps(status.to_dict()))
        return 0
    if command == "run-operation-start":
        try:
            input_data = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
            result = deps.build_workflow_operation_service(config).begin_operation(
                args.external_id,
                args.invocation_id,
                args.operation,
                input_data,
            )
        except Exception as exc:
            raise SystemExit(f"workflow operation start failed: {exc}") from exc
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-operation-finish":
        output = None
        if args.output_file:
            output = json.loads(Path(args.output_file).read_text(encoding="utf-8"))
        try:
            result = deps.build_workflow_operation_service(config).complete_operation(
                args.external_id,
                args.invocation_id,
                succeeded=args.status == "succeeded",
                output=output,
                error=args.error,
            )
        except Exception as exc:
            raise SystemExit(f"workflow operation finish failed: {exc}") from exc
        print(deps.dumps(result.to_dict()))
        return 0

    run_service = None
    status = None
    expected_revision = None
    if command in {
        "run-status",
        "run-mode-change",
        "run-transition",
        "run-reopen",
        "run-cancel",
    }:
        run_service = deps.build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        if command == "run-status":
            print(deps.dumps(status.to_dict()))
            return 0
        expected_revision = (
            args.expected_revision
            if args.expected_revision is not None
            else status.lifecycle_revision
        )
    if command == "run-mode-change":
        result = run_service.change_execution_mode(
            status.id,
            args.mode,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            reason=args.reason,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
        )
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-transition":
        result = run_service.transition(
            status.id,
            args.next_state,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
            semantic_proposal_id=(
                UUID(args.semantic_proposal_id) if args.semantic_proposal_id else None
            ),
            reason=args.reason,
        )
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-finish":
        try:
            result = deps.build_workflow_operation_service(config).finish_run(
                args.external_id,
                outcome=args.outcome,
                status_name=args.status,
                source_manifest_sha256=args.source_manifest_sha256,
                answer_sha256=args.answer_sha256,
                provenance_type=args.provenance_type,
                idempotency_key=args.idempotency_key,
            )
        except Exception as exc:
            raise SystemExit(f"run finish failed: {exc}") from exc
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-reopen":
        result = run_service.reopen(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:reopen:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-cancel":
        result = run_service.cancel(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:cancel:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(deps.dumps(result.to_dict()))
        return 0
    if command == "run-annotate":
        run_service = deps.build_run_service(config)
        try:
            status = run_service.status(external_id=args.external_id)
        except KeyError:
            print(f"error: run {args.external_id} not found", file=sys.stderr)
            return 1
        expected_revision = (
            args.expected_revision
            if args.expected_revision is not None
            else status.lifecycle_revision
        )
        result = run_service.annotate(
            status.id,
            event_type=args.type,
            reason=args.reason,
            from_invocation=args.from_invocation,
            to_invocation=args.to_invocation,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:annotate:{args.external_id}:{args.type}:{args.reason}",
            actor_type=args.actor,
        )
        print(deps.dumps(result))
        return 0
    if command == "run-verify":
        run_service = deps.build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        result = run_service.verify(status.id)
        if args.output == "-":
            print(deps.dumps(result))
        else:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, dir=str(Path(args.output).parent)
            ) as file:
                json.dump(result, file, indent=2, sort_keys=True)
                file.write("\n")
            print(deps.dumps({"status": "written", "path": file.name}))
        if result["status"] == "inconclusive" and not args.allow_empty:
            return 1
        return 0
    if command == "run-audit":
        run_service = deps.build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        target_hash = args.target_hash
        if not target_hash:
            from ..assessment.audit_packet import compute_audit_packet_hash_from_db

            target_hash = compute_audit_packet_hash_from_db(
                status.id, run_service.uow_factory
            )
        model = args.model or "default"
        result = run_service.trigger_audit(
            status.id,
            target_hash=target_hash,
            provider=args.llm,
            model=model,
            force=args.force,
            stages=args.stages.split(",") if args.stages else None,
            max_calls=args.max_calls,
            max_input_tokens=args.max_input_tokens,
            fallback_provider=args.commercial_fallback,
            fallback_model=args.fallback_model,
        )
        print(deps.dumps(result))
        return 0
    if command == "run-compare":
        run_service = deps.build_run_service(config)
        results = []
        for external_id in args.external_ids:
            run_status = run_service.status(external_id=external_id)
            results.append(run_status.to_dict())
        print(deps.dumps({"comparison": results, "count": len(results)}))
        return 0
    raise AssertionError(command)
