"""Application assembly for acquisition-related administrative commands."""

from __future__ import annotations

import json
from pathlib import Path


def record_budget(config, args, deps) -> dict:
    """Persist one validated ResearchSpec/budget snapshot pair."""
    from firecrawl_skill.research_domain import load_model, serialize_model
    from firecrawl_skill.research_domain.models import ResearchSpec

    spec_payload = json.loads(Path(args.research_spec).read_text(encoding="utf-8"))
    spec = load_model(spec_payload)
    if not isinstance(spec, ResearchSpec):
        raise SystemExit("--research-spec must contain research-spec-v1")
    snapshot = json.loads(Path(args.budget_snapshot).read_text(encoding="utf-8"))
    required = {
        "snapshot_version",
        "policy_version",
        "policy_config_sha256",
        "research_spec_id",
        "spec_revision",
        "run_revision",
        "effective_caps",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise SystemExit(f"budget snapshot missing required fields: {missing}")
    if snapshot["research_spec_id"] != str(spec.research_spec_id):
        raise SystemExit("budget snapshot references another ResearchSpec")
    run_id = deps._resolve_run_id(config, args.external_id)
    with deps._uow_factory(config)() as uow:
        spec_id = uow.runs.record_research_spec(
            run_id,
            snapshot["spec_revision"],
            "research-spec",
            1,
            serialize_model(spec),
            f"research-spec:{spec.research_spec_id}:r{snapshot['spec_revision']}",
        )
        budget_id = uow.runs.record_budget_snapshot(
            run_id,
            spec_id,
            snapshot["spec_revision"],
            snapshot["run_revision"],
            snapshot["policy_version"],
            snapshot["policy_config_sha256"],
            snapshot,
            "budget:"
            f"{snapshot['policy_version']}:r{snapshot['run_revision']}:"
            f"{spec.research_spec_id}",
        )
    return {"id": budget_id, "external_run_id": args.external_id}
