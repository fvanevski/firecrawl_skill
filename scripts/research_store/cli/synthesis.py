from __future__ import annotations

from .. import synthesis_admin
from ..report_service import CommercialFallbackError, ReportServiceError

COMMANDS = {"synthesis-run", "synthesis-status", "synthesis-resume"}


def run(args, config, deps):
    try:
        result = synthesis_admin.execute(
            config,
            args,
            build_run_service=deps.build_run_service,
            resolve_run_id=deps._resolve_run_id,
            build_resource_governor=deps.build_resource_governor,
        )
        print(deps.dumps(result))
        return None
    except CommercialFallbackError:
        raise SystemExit(
            "commercial fallback not permitted. Use --commercial-fallback to enable."
        )
    except ReportServiceError as exc:
        label = "synthesis" if args.command == "synthesis-run" else "synthesis resume"
        raise SystemExit(f"{label} failed: {exc}") from exc
