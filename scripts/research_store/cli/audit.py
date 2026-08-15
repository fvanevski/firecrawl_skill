from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

COMMANDS = {"audit", "audit-status", "audit-query", "audit-export", "audit-staleness"}


def run(args, config, deps):
    command = args.command
    config.require_database()
    audit_svc = deps.build_audit_service(config)
    if command == "audit":
        run_id = deps._resolve_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(
                f"research run not found or not running: {args.external_id}"
            )
        stage_set = [stage.strip() for stage in args.stages.split(",") if stage.strip()]
        manifest = None
        if args.packet_manifest_file:
            with open(args.packet_manifest_file, "r") as file:
                manifest = json.load(file)
        assessment = audit_svc.assess_run(
            run_id=run_id,
            external_run_id=args.external_id,
            target_hash=args.target_hash,
            evaluator_version=args.evaluator_version,
            prompt_template_version=args.prompt_template_version,
            policy_version=args.policy_version,
            stage_set=stage_set,
            status=args.status,
            provider=args.provider,
            model=args.model,
            prompt_hash=args.prompt_hash,
            model_fingerprint=args.model_fingerprint,
            elapsed_ms=args.elapsed_ms,
            audit_packet_manifest=manifest,
        )
        print(deps.dumps(assessment))
        return None
    if command == "audit-status":
        run_id = deps._resolve_any_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(f"research run not found: {args.external_id}")
        assessments = audit_svc.list_assessments(run_id=run_id, limit=1, offset=0)
        result = assessments[0] if assessments else None
        if result is None:
            raise SystemExit(f"no assessments found for run: {args.external_id}")
        print(deps.dumps(result))
        return None
    if command == "audit-query":
        run_id = deps._resolve_any_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(f"research run not found: {args.external_id}")
        assessments = audit_svc.list_assessments(
            run_id=run_id,
            status=args.status_filter,
            limit=args.limit,
            offset=args.offset,
        )
        print(deps.dumps({"run_id": str(run_id), "assessments": assessments}))
        return None
    if command == "audit-export":
        export = audit_svc.export_assessment(UUID(args.assessment_id))
        if export is None:
            raise SystemExit(f"assessment not found: {args.assessment_id}")
        if args.output == "-":
            print(deps.dumps(export))
        else:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(export, file, indent=2, default=deps.json_default)
                os.replace(tmp_path, str(output_path))
            except BaseException:
                os.unlink(tmp_path)
                raise
        return None
    if command == "audit-staleness":
        run_id = deps._resolve_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(
                f"research run not found or not running: {args.external_id}"
            )
        stale = audit_svc.detect_stale_assessments(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            current_hash=args.target_hash,
        )
        print(deps.dumps({"run_id": str(run_id), "stale_assessments": stale}))
        return None
    raise AssertionError(command)
