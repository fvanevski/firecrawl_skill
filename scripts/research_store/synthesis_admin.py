"""Application assembly for synthesis CLI commands."""

from __future__ import annotations

from .report_service import LocalSynthesisService
from .semantic_service import SemanticCallService


def execute(
    config, args, *, build_run_service, resolve_run_id, build_resource_governor
):
    config.require_database()
    run_service = build_run_service(config)
    run_id = resolve_run_id(config, args.external_id)
    if run_id is None:
        raise SystemExit(f"research run not found: {args.external_id}")
    semantic_service = SemanticCallService(run_service.uow_factory)

    if args.command == "synthesis-status":
        report_service = LocalSynthesisService(
            semantic_service=semantic_service,
            evidence_service=run_service.evidence_service,
            config=config,
        )
        stages = report_service.get_stage_status(
            uow_factory=run_service.uow_factory,
            run_id=run_id,
            stage_name=args.stage,
        )
        return {"run_id": str(run_id), "stages": stages}

    governor = None
    if args.command == "synthesis-run":
        try:
            governor = build_resource_governor(config)
        except Exception:  # noqa: BLE001
            governor = None
    report_service = LocalSynthesisService(
        semantic_service=semantic_service,
        evidence_service=run_service.evidence_service,
        config=config,
        **({"resource_governor": governor} if args.command == "synthesis-run" else {}),
    )
    kwargs = {
        "run_id": run_id,
        "packet_revision": args.packet_revision or 1,
        "model_name": args.model or config.generative_model,
        "prompt_version": args.prompt_version,
        "allow_commercial_fallback": args.commercial_fallback is not None,
    }
    if args.command == "synthesis-run":
        return report_service.run_synthesis(**kwargs)
    return report_service.resume_failed_synthesis(**kwargs)
