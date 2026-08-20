from __future__ import annotations

import argparse


def parser():
    root = argparse.ArgumentParser(
        prog="research-db", description="Authoritative research asset store"
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("ingest-ready")

    sub.add_parser("endpoint-health")
    sub.add_parser("resource-status")

    bench = sub.add_parser("benchmark", help="Run release benchmark campaign")
    bench_sub = bench.add_subparsers(dest="benchmark_subcommand", required=True)
    bench_run = bench_sub.add_parser(
        "run",
        help=(
            "Run benchmark against fixed dataset. "
            "Exit code: 0 = go or go_with_conditions (both are successes), "
            "2 = no_go (failure). "
            "(P7-07: exit code changed from 0=go/1=go_with_conditions/2=no_go "
            "to 0=go_or_go_with_conditions/2=no_go; parse the JSON 'outcome' "
            "field to distinguish between go and go_with_conditions.)"
        ),
    )
    bench_run.add_argument(
        "--dataset",
        dest="benchmark_dataset",
        required=True,
        help="Path to benchmark dataset JSON file",
    )
    bench_run.add_argument(
        "--modes",
        dest="benchmark_modes",
        nargs="+",
        help="Workflow modes to benchmark (default: from dataset)",
    )
    bench_run.add_argument(
        "--no-dry-run",
        dest="benchmark_no_dry_run",
        action="store_true",
        help="Run with actual workflow execution (not simulation)",
    )
    bench_run.add_argument(
        "--blob-root",
        dest="benchmark_blob_root",
        help="Path to content-addressed blob store root for integrity checks",
    )
    bench_run.add_argument(
        "--output",
        dest="benchmark_output",
        help="Path to write benchmark results JSON",
    )
    bench_results = bench_sub.add_parser(
        "results", help="Display saved benchmark results"
    )
    bench_results.add_argument(
        "--results-path",
        dest="benchmark_results_path",
        required=True,
        help="Path to saved benchmark results JSON",
    )
    bench_report = bench_sub.add_parser(
        "report", help="Generate human-readable benchmark report"
    )
    bench_report.add_argument(
        "--results-path",
        dest="benchmark_report_path",
        required=True,
        help="Path to benchmark results JSON",
    )
    bench_report.add_argument(
        "--output",
        dest="benchmark_report_output",
        help="Path to write report text file",
    )

    sub.add_parser("parser-info", help="Show parser registry information")

    ingest = sub.add_parser("ingest-result")
    ingest.add_argument("--url", required=True)
    ingest.add_argument("--file", required=True)
    ingest.add_argument("--title")
    ingest.add_argument("--metadata-json", default="{}")
    sub.add_parser("verify-blobs")

    worker = sub.add_parser("worker")
    worker.add_argument("--batch-size", type=int, default=32)
    worker.add_argument("--poll-seconds", type=float)
    worker.add_argument("--lease-seconds", type=int)
    worker.add_argument("--max-attempts", type=int)
    worker.add_argument("--once", action="store_true")
    once = sub.add_parser("index-once")
    once.add_argument("--limit", type=int, default=64)

    sub.add_parser("index-list")
    build = sub.add_parser("index-build")
    build.add_argument("--current-config", action="store_true", required=True)
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document")
    reindex = sub.add_parser("reindex")
    selection = reindex.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document")
    activate = sub.add_parser("index-activate")
    activate.add_argument("id")
    rollback = sub.add_parser("index-rollback")
    rollback.add_argument("id")
    prune = sub.add_parser("index-prune")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--force", action="store_true")
    prune.add_argument("--keep-last", type=int, default=2)
    prune.add_argument("--index-id")
    sub.add_parser("reconcile-qdrant")
    reconcile = sub.add_parser("index-reconcile")
    reconcile.add_argument("--repair", action="store_true")
    sub.add_parser("prune-cache")

    rederive = sub.add_parser("rederive")
    target = rederive.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true")
    target.add_argument("--snapshot")

    rederive_v2 = sub.add_parser(
        "rederive-v2",
        help=(
            "Redrive document derivation with explicit version selection (issue #47)"
        ),
    )
    target_v2 = rederive_v2.add_mutually_exclusive_group(required=True)
    target_v2.add_argument("--all", action="store_true", help="Redrive all documents")
    target_v2.add_argument("--snapshot", help="Snapshot UUID to rederive")
    target_v2.add_argument("--document", help="Document UUID to rederive")
    rederive_v2.add_argument(
        "--parser-version",
        help="Explicit parser version (overrides config default)",
    )
    rederive_v2.add_argument(
        "--normalization-version",
        help="Explicit normalization version (overrides config default)",
    )
    rederive_v2.add_argument(
        "--chunker-version",
        help="Explicit chunker version (overrides config default)",
    )
    rederive_v2.add_argument(
        "--chunker-name",
        help="Explicit chunker name (overrides config default)",
    )
    rederive_v2.add_argument(
        "--tokenizer-name",
        help="Explicit tokenizer name (overrides config default)",
    )
    rederive_v2.add_argument(
        "--activate",
        action="store_true",
        help="Activate the new derivation after successful rederive",
    )
    rederive_v2.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute what would happen without writing",
    )
    rederive_v2.add_argument(
        "--report",
        help="Path to write the derivation report as JSON",
    )

    deriv_list = sub.add_parser(
        "derivation-list",
        help="List document derivations",
    )
    deriv_list.add_argument("--document", help="Filter by document UUID")
    deriv_list.add_argument("--snapshot", help="Filter by snapshot UUID")
    deriv_list.add_argument("--status", help="Filter by status")

    deriv_activate = sub.add_parser(
        "derivation-activate",
        help="Activate a pending derivation",
    )
    deriv_activate.add_argument("id", help="Derivation UUID to activate")
    deriv_activate.add_argument(
        "--document",
        help="Document UUID to validate derivation ownership before activation",
    )

    deriv_compare = sub.add_parser(
        "derivation-compare",
        help="Compare two derivations",
    )
    deriv_compare.add_argument("old_id", help="Old derivation UUID")
    deriv_compare.add_argument("new_id", help="New derivation UUID")
    deriv_compare.add_argument(
        "--output", default="-", help="Output file (default: stdout)"
    )

    norm = sub.add_parser("normalize", help="Run normalization and show diagnostics")
    norm.add_argument("--document", help="Document UUID to normalize")
    norm.add_argument("--all", action="store_true", help="Normalize all documents")
    norm.add_argument(
        "--aggressive", action="store_true", help="Enable aggressive cleanup"
    )
    norm.add_argument(
        "--document-type",
        default="web",
        choices=["web", "academic", "legal", "documentation"],
    )

    export = sub.add_parser("export-invocation")
    export.add_argument("invocation_id")
    export.add_argument("--output", required=True)
    export_run = sub.add_parser("export-run")
    export_run.add_argument("id")
    export_run.add_argument("--output", required=True)
    export_run.add_argument(
        "--schema-version",
        default="export-run-v2",
        help="Export schema version",
    )
    integrity = sub.add_parser("integrity")
    integrity.add_argument("id")
    integrity.add_argument("--output", required=True)
    integrity.add_argument(
        "--schema-version",
        default="integrity-v1",
        help="Integrity report schema version",
    )

    run_start = sub.add_parser("run-start")
    run_start.add_argument("external_id")
    run_start.add_argument("objective")
    run_start.add_argument(
        "--mode",
        choices=("agent_led", "autonomous_local", "deterministic_debug"),
        default="autonomous_local",
    )
    run_start.add_argument("--idempotency-key")
    run_start.add_argument("--actor", default="cli")
    run_status = sub.add_parser("run-status")
    run_status.add_argument("external_id")
    run_operation_start = sub.add_parser("run-operation-start")
    run_operation_start.add_argument("external_id")
    run_operation_start.add_argument("invocation_id")
    run_operation_start.add_argument("operation", choices=("fsearch", "fscrape"))
    run_operation_start.add_argument("--input-file", required=True)
    run_operation_finish = sub.add_parser("run-operation-finish")
    run_operation_finish.add_argument("external_id")
    run_operation_finish.add_argument("invocation_id")
    run_operation_finish.add_argument(
        "--status", choices=("succeeded", "failed"), required=True
    )
    run_operation_finish.add_argument("--output-file")
    run_operation_finish.add_argument("--error")
    run_mode = sub.add_parser("run-mode-change")
    run_mode.add_argument("external_id")
    run_mode.add_argument(
        "mode", choices=("agent_led", "autonomous_local", "deterministic_debug")
    )
    run_mode.add_argument("--expected-revision", type=int, required=True)
    run_mode.add_argument("--idempotency-key", required=True)
    run_mode.add_argument("--requested-by", required=True)
    run_mode.add_argument("--approved-by", required=True)
    run_mode.add_argument("--reason", required=True)
    run_mode.add_argument("--actor", default="operator")
    run_mode.add_argument("--actor-identifier")
    run_transition = sub.add_parser("run-transition")
    run_transition.add_argument("external_id")
    run_transition.add_argument("next_state")
    run_transition.add_argument("--expected-revision", type=int, required=True)
    run_transition.add_argument("--idempotency-key", required=True)
    run_transition.add_argument("--actor", default="cli")
    run_transition.add_argument("--actor-identifier")
    run_transition.add_argument("--semantic-proposal-id")
    run_transition.add_argument("--reason")
    run_finish = sub.add_parser("run-finish")
    run_finish.add_argument("external_id")
    run_finish.add_argument("--outcome", required=True)
    run_finish.add_argument(
        "--status", choices=("complete", "failed"), default="complete"
    )
    run_finish.add_argument("--source-manifest-sha256")
    run_finish.add_argument("--answer-sha256")
    run_finish.add_argument("--provenance-type")
    run_finish.add_argument("--idempotency-key")
    run_reopen = sub.add_parser("run-reopen")
    run_reopen.add_argument("external_id")
    run_reopen.add_argument("--reason", default="operator requested reopen")
    run_reopen.add_argument("--expected-revision", type=int)
    run_reopen.add_argument("--idempotency-key")
    run_reopen.add_argument("--actor", default="cli")
    run_cancel = sub.add_parser("run-cancel")
    run_cancel.add_argument("external_id")
    run_cancel.add_argument("--reason", default="cancelled by operator")
    run_cancel.add_argument("--expected-revision", type=int)
    run_cancel.add_argument("--idempotency-key")
    run_cancel.add_argument("--actor", default="cli")

    run_annotate = sub.add_parser("run-annotate")
    run_annotate.add_argument("external_id")
    run_annotate.add_argument(
        "--type", choices=("pivot", "retry", "decision"), required=True
    )
    run_annotate.add_argument("--reason", required=True)
    run_annotate.add_argument("--from-invocation")
    run_annotate.add_argument("--to-invocation")
    run_annotate.add_argument("--expected-revision", type=int)
    run_annotate.add_argument("--idempotency-key")
    run_annotate.add_argument("--actor", default="cli")
    run_verify = sub.add_parser("run-verify")
    run_verify.add_argument("external_id")
    run_verify.add_argument("--output", default="-")
    run_verify.add_argument(
        "--allow-empty",
        action="store_true",
        default=False,
        help="Exit 0 even when verification is inconclusive (no eligible objects)",
    )
    run_audit = sub.add_parser("run-audit")
    run_audit.add_argument("external_id")
    run_audit.add_argument("--target-hash")
    run_audit.add_argument(
        "--llm", choices=("local", "openai", "gemini"), default="local"
    )
    run_audit.add_argument("--model")
    run_audit.add_argument("--force", action="store_true")
    run_audit.add_argument("--stages")
    run_audit.add_argument("--max-calls", type=int)
    run_audit.add_argument("--max-input-tokens", type=int)
    run_audit.add_argument("--commercial-fallback", choices=("openai", "gemini"))
    run_audit.add_argument("--fallback-model")
    run_compare = sub.add_parser("run-compare")
    run_compare.add_argument("external_ids", nargs="+")
    budget_record = sub.add_parser("budget-record")
    budget_record.add_argument("external_id")
    budget_record.add_argument("--research-spec", required=True)
    budget_record.add_argument("--budget-snapshot", required=True)

    search_plan_rec = sub.add_parser("search-plan-record")
    search_plan_rec.add_argument("external_id")
    search_plan_rec.add_argument("--research-spec-id", required=True)
    search_plan_rec.add_argument("--revision", type=int, required=True)
    search_plan_rec.add_argument("--search-plan", required=True)
    search_plan_rec.add_argument("--idempotency-key", required=True)
    search_plan_get = sub.add_parser("search-plan-get")
    search_plan_get.add_argument("external_id")
    search_plan_get.add_argument("--plan-id")
    search_plan_get.add_argument("--revision", type=int)
    plan_query_get = sub.add_parser("search-plan-query-get")
    plan_query_get.add_argument("query_id")

    search_resp_rec = sub.add_parser("search-response-record")
    search_resp_rec.add_argument("external_id")
    search_resp_rec.add_argument("--query-text", required=True)
    search_resp_rec.add_argument("--backend", default="firecrawl")
    search_resp_rec.add_argument(
        "--payload-file", help="Path to raw payload file (reads stdin if omitted)"
    )
    search_resp_rec.add_argument("--idempotency-key", required=True)
    search_resp_rec.add_argument("--plan-id")
    search_resp_rec.add_argument("--plan-query-id")
    search_resp_rec.add_argument("--provider-request-id")
    search_resp_rec.add_argument("--parser-version", default="firecrawl-search-v1")
    search_resp_rec.add_argument("--http-status", type=int)
    search_resp_get = sub.add_parser("search-response-get")
    search_resp_get.add_argument("response_id")
    search_resp_replay = sub.add_parser("search-response-replay")
    search_resp_replay.add_argument("response_id")

    cand_rec_resp = sub.add_parser("candidate-record-response")
    cand_rec_resp.add_argument("external_id")
    cand_rec_resp.add_argument("--search-response-id", required=True)
    cand_get = sub.add_parser("candidate-get")
    cand_get.add_argument("candidate_id")
    cand_list = sub.add_parser("candidate-list")
    cand_list.add_argument("external_id")
    cand_list.add_argument("--domain")
    cand_list.add_argument("--min-recurrence", type=int)
    cand_list.add_argument("--duplicate-group-id")
    cand_occ_list = sub.add_parser("candidate-occurrences-list")
    cand_occ_list.add_argument("candidate_id")
    cand_grp = sub.add_parser("candidate-assign-group")
    cand_grp.add_argument("candidate_ids", nargs="+")
    cand_grp.add_argument("--group-id")

    acq_search = sub.add_parser("acquisition-search")
    acq_search.add_argument("external_id")
    acq_search.add_argument("query_text")
    acq_search.add_argument("--backend", default="firecrawl")
    acq_search.add_argument("--limit", type=int, default=20)
    acq_search.add_argument("--sources", default="web")
    acq_search.add_argument("--tbs")
    acq_search.add_argument("--plan-id")
    acq_search.add_argument("--plan-query-id")
    acq_search.add_argument("--idempotency-key")
    acq_recon = sub.add_parser("acquisition-reconcile")
    acq_recon.add_argument("external_id")

    cand_list_pag = sub.add_parser("candidate-list-paginated")
    cand_list_pag.add_argument("external_id")
    cand_list_pag.add_argument("--plan-id")
    cand_list_pag.add_argument("--plan-query-id")
    cand_list_pag.add_argument("--query-text")
    cand_list_pag.add_argument("--domain")
    cand_list_pag.add_argument("--min-recurrence", type=int)
    cand_list_pag.add_argument("--duplicate-group-id")
    cand_list_pag.add_argument("--limit", type=int, default=20)
    cand_list_pag.add_argument("--offset", type=int, default=0)
    cand_card = sub.add_parser("candidate-card")
    cand_card.add_argument("candidate_id")
    cand_card.add_argument("--max-snippet-length", type=int, default=500)
    cand_triage = sub.add_parser("candidate-triage-input")
    cand_triage.add_argument("external_id")
    cand_triage.add_argument("--plan-id")
    cand_triage.add_argument("--plan-query-id")
    cand_triage.add_argument("--query-text")
    cand_triage.add_argument("--domain")
    cand_triage.add_argument("--min-recurrence", type=int)
    cand_triage.add_argument("--duplicate-group-id")
    cand_triage.add_argument("--limit", type=int, default=50)
    cand_triage.add_argument("--offset", type=int, default=0)
    cand_triage.add_argument("--max-snippet-length", type=int, default=500)
    cand_replay = sub.add_parser("candidate-replay")
    cand_replay.add_argument("external_id")
    cand_replay.add_argument("--plan-id")
    cand_replay.add_argument("--plan-query-id")
    cand_replay.add_argument("--domain")
    cand_replay.add_argument("--min-recurrence", type=int)
    cand_replay.add_argument("--limit", type=int, default=100)
    cand_replay.add_argument("--offset", type=int, default=0)

    sub.add_parser("corpus-overview")
    search = sub.add_parser("search-assets")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument(
        "--mode", default="hybrid", choices=["hybrid", "lexical", "semantic"]
    )
    search.add_argument("--domain")
    search.add_argument("--source-type")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    search.add_argument("--research-run-id")
    inspect = sub.add_parser("inspect-asset")
    inspect.add_argument("id")
    fetch = sub.add_parser("fetch-passages")
    fetch.add_argument("ids", nargs="+")
    fetch.add_argument("--max-tokens", type=int, default=2000)
    fetch.add_argument("--max-passages", type=int, default=8)
    fetch.add_argument("--research-run-id")
    expand = sub.add_parser("expand-relationships")
    expand.add_argument("ids", nargs="+")
    expand.add_argument("--max-hops", type=int, default=1)
    expand.add_argument("--max-results", type=int, default=50)
    expand.add_argument("--max-tokens", type=int, default=2000)
    packet = sub.add_parser("build-evidence-packet")
    packet.add_argument("ids", nargs="+")
    packet.add_argument("--max-tokens", type=int, default=3000)

    packet_validate = sub.add_parser("packet-validate")
    packet_validate.add_argument("run_id")
    packet_validate.add_argument("--revision", type=int, default=None)
    packet_validate.add_argument("--output", default="-")
    packet_validate.add_argument("--include-warnings", action="store_true")
    packet_inspect = sub.add_parser("packet-inspect")
    packet_inspect.add_argument("run_id")
    packet_inspect.add_argument("--revision", type=int, default=None)
    packet_inspect.add_argument("--output", default="-")
    packet_inspect.add_argument("--bounded", action="store_true")
    packet_inspect.add_argument("--max-passages", type=int, default=20)
    packet_inspect.add_argument("--max-claims", type=int, default=10)
    packet_diff = sub.add_parser("packet-diff")
    packet_diff.add_argument("run_id")
    packet_diff.add_argument("--old-revision", type=int, required=True)
    packet_diff.add_argument("--new-revision", type=int, required=True)
    packet_diff.add_argument("--output", default="-")
    packet_export = sub.add_parser("packet-export")
    packet_export.add_argument("run_id")
    packet_export.add_argument("--revision", type=int, default=None)
    packet_export.add_argument("--output", required=True)
    packet_export.add_argument("--bounded", action="store_true")
    packet_export.add_argument("--max-passages", type=int, default=20)
    packet_export.add_argument("--max-claims", type=int, default=10)

    handoff = sub.add_parser(
        "handoff",
        help="Produce a bounded host-agent handoff payload (issue #62)",
    )
    handoff.add_argument("run_id", help="Research run UUID")
    handoff.add_argument(
        "--output",
        default="-",
        help="Output file (default: stdout)",
    )
    handoff.add_argument(
        "--max-passages",
        type=int,
        default=128,
        help="Max passages in citation-ready output (default: 128)",
    )
    handoff.add_argument(
        "--max-claims",
        type=int,
        default=64,
        help="Max claims in citation-ready output (default: 64)",
    )
    handoff.add_argument(
        "--token-limit-max-input",
        type=int,
        default=None,
        help="Override max_input_tokens cap",
    )
    handoff.add_argument(
        "--token-limit-max-output",
        type=int,
        default=None,
        help="Override max_output_tokens cap",
    )
    handoff.add_argument(
        "--token-limit-max-retrieval",
        type=int,
        default=None,
        help="Override max_retrieval_candidates cap",
    )

    claim = sub.add_parser("claim-manifest")
    claim_sub = claim.add_subparsers(dest="claim_command", required=True)
    claim_import = claim_sub.add_parser("import")
    claim_import.add_argument("external_id")
    claim_import.add_argument("--file", required=True)
    claim_import.add_argument("--dry-run", action="store_true")
    claim_export = claim_sub.add_parser("export")
    claim_export.add_argument("external_id")
    claim_export.add_argument("--output", required=True)
    claim_list = claim_sub.add_parser("list")
    claim_list.add_argument("external_id")

    audit = sub.add_parser("audit")
    audit.add_argument("external_id")
    audit.add_argument("--target-hash", required=True)
    audit.add_argument("--evaluator-version", default="research-audit-v1")
    audit.add_argument("--prompt-template-version", default="staged-research-audit-v1")
    audit.add_argument("--policy-version", default="audit-policy-v1")
    audit.add_argument("--stages", default="rubric,acquisition,evidence,synthesis")
    audit.add_argument(
        "--status", default="partial", choices=["completed", "partial", "failed"]
    )
    audit.add_argument("--provider", default="local")
    audit.add_argument("--model")
    audit.add_argument("--prompt-hash")
    audit.add_argument("--model-fingerprint", required=True)
    audit.add_argument("--elapsed-ms", type=int, default=0)
    audit.add_argument("--packet-manifest-file")
    audit_status = sub.add_parser("audit-status")
    audit_status.add_argument("external_id")
    audit_status.description = "Show the latest audit assessment for a research run"
    audit_query = sub.add_parser("audit-query")
    audit_query.add_argument("external_id")
    audit_query.add_argument("--status-filter")
    audit_query.add_argument("--limit", type=int, default=100)
    audit_query.add_argument("--offset", type=int, default=0)
    audit_export = sub.add_parser("audit-export")
    audit_export.add_argument("assessment_id")
    audit_export.add_argument("--output", default="-")
    audit_staleness = sub.add_parser("audit-staleness")
    audit_staleness.add_argument("external_id")
    audit_staleness.add_argument("--target-hash", required=True)

    synthesis_run = sub.add_parser("synthesis-run")
    synthesis_run.add_argument("external_id")
    synthesis_run.add_argument("--packet-revision", type=int, default=None)
    synthesis_run.add_argument("--model")
    synthesis_run.add_argument("--prompt-version", default="synthesis-v1")
    synthesis_run.add_argument(
        "--commercial-fallback",
        choices=("openai", "gemini"),
        default=None,
        help="Allow commercial LLM fallback (not recommended for production).",
    )
    synthesis_status = sub.add_parser("synthesis-status")
    synthesis_status.add_argument("external_id")
    synthesis_status.add_argument("--stage", default=None, help="Filter by stage name")
    synthesis_resume = sub.add_parser("synthesis-resume")
    synthesis_resume.add_argument("external_id")
    synthesis_resume.add_argument("--packet-revision", type=int, default=None)
    synthesis_resume.add_argument("--model")
    synthesis_resume.add_argument("--prompt-version", default="synthesis-v1")
    synthesis_resume.add_argument(
        "--commercial-fallback",
        choices=("openai", "gemini"),
        default=None,
        help="Allow commercial LLM fallback (not recommended for production).",
    )

    return root
