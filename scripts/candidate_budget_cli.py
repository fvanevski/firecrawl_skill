"""Operator CLI for authoritative candidate-budget checks and soft overrides."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from firecrawl_skill.research_store.candidate_policy_service import (
        CandidatePolicyService,
    )
    from firecrawl_skill.research_store.config import StoreConfig
    from firecrawl_skill.research_store.run_service import ResearchRunService


def _require_authoritative_wrapper() -> None:
    if os.environ.get("FIRECRAWL_CANDIDATE_BUDGET_WRAPPER") == "1":
        return
    print(
        "ERROR: candidate_budget_cli.py is an internal entry point; "
        "use scripts/candidate-budget so research-env provenance is enforced",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _service(config: StoreConfig) -> tuple[ResearchRunService, CandidatePolicyService]:
    from firecrawl_skill.research_store.candidate_policy_service import (
        CandidatePolicyService,
    )
    from firecrawl_skill.research_store.composition import build_run_service

    run_service = build_run_service(config)
    return run_service, CandidatePolicyService(run_service.uow_factory)


def _run_id(run_service: ResearchRunService, external_id: str) -> UUID:
    return UUID(str(run_service.status(external_id=external_id).id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candidate-budget")
    sub = parser.add_subparsers(dest="command", required=True)

    checks = sub.add_parser("checks", help="list persisted budget checks")
    checks.add_argument("research_run_id")

    override = sub.add_parser(
        "override", help="record an explicit justification for one soft violation"
    )
    override.add_argument("research_run_id")
    override.add_argument("budget_check_id")
    override.add_argument("limit_name")
    override.add_argument("--reason", required=True)
    override.add_argument("--author", required=True)

    config = sub.add_parser("config", help="show effective candidate budget")
    config.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    _require_authoritative_wrapper()
    from firecrawl_skill.research_store.acquisition.candidate_ranking import (
        CandidateBudget,
    )
    from firecrawl_skill.research_store.config import StoreConfig

    args = build_parser().parse_args(argv)
    if args.command == "config":
        value = CandidateBudget.from_env().to_dict()
        print(json.dumps(value, sort_keys=True) if args.json_output else value)
        return 0

    config = StoreConfig.from_env()
    config.require_database()
    run_service, policy = _service(config)
    run_id = _run_id(run_service, args.research_run_id)
    if args.command == "checks":
        print(json.dumps(policy.list_checks(run_id), default=str, sort_keys=True))
        return 0
    if args.command == "override":
        override_id = policy.record_override(
            run_id,
            UUID(args.budget_check_id),
            args.limit_name,
            reason=args.reason,
            author=args.author,
        )
        print(
            json.dumps(
                {
                    "schema_version": "candidate-budget-override-v1",
                    "run_id": str(run_id),
                    "budget_check_id": args.budget_check_id,
                    "override_id": str(override_id),
                    "limit_name": args.limit_name,
                },
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
