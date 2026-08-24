# Issue #305 audit-remediation operator contract

This reference records the user-visible contracts added by issue #305. It does
not change the repository authority model: PostgreSQL remains authoritative for
workflow/corpus identity and exact membership, `BLOB_ROOT` for immutable bytes,
Qdrant for rebuildable vector projection, and Valkey for transient coordination.

## Runtime provenance

Every bundled shell wrapper that sources `scripts/research-env` is bound to the
checkout containing that wrapper. The selected `FIRECRAWL_RESEARCH_PYTHON`
interpreter must naturally import `firecrawl_skill` from
`<skill-root>/src/firecrawl_skill`. An executable that imports another checkout
or installed package is rejected before database/provider work. The canonical
checkout-local environment is `<skill-root>/.venv-research-store/bin/python`.
The contract test executes a real temporary virtual environment at that exact
path and proves its natural import resolves to the checkout under test; sandboxed
`.env` tests provide their own provenance-valid interpreter fixture instead of
bypassing the guard.

## Smart-run disposition and exact soft-gate recovery

`scripts/fsearch_smart` prints `Run ID`, `Final state`, `Orchestrator outcome`,
and `Next action` for every completed invocation.

- `completed` and terminal `partial` return `0`.
- `checkpoint`, `resumable`, and `operator_action_required` return `75` and must
  continue the same run only after the printed next action is satisfied.
- `failed`, `cancelled`, an explicit error, and an unrecognized nonterminal
  result return non-zero.

A soft completion-admission candidate-budget gate raises a typed boundary from
the exact persisted decision that failed. The boundary carries run ID, lifecycle
revision, check ID, scope, fingerprint, and unresolved limits. The indexing stage
propagates only that exact typed boundary; it never reinterprets an unrelated
`AssetPromotionError` by consulting an older same-revision soft check.

The resulting smart-run state is `operator_action_required` without claiming
membership sealing or indexing completion, and the CLI exits `75` with canonical
recovery commands:

```bash
scripts/candidate-budget checks <fr_run_id>
scripts/candidate-budget override <fr_run_id> <check_uuid> <soft_limit_name> \
  --reason '<justification>' --author '<operator>'
scripts/fsearch_smart '<same objective>' --research-run-id <fr_run_id>
```

Overrides remain bound to the exact persisted check, lifecycle revision, scope,
and membership fingerprint. Hard violations cannot be overridden. A membership
change requires a new exact completion-admission decision.

## Candidate-budget operator boundary

`scripts/candidate-budget` is the only executable operator entry point. It
sources `scripts/research-env` and then executes the package module
`firecrawl_skill.research_store.candidate_budget_cli` with the provenance-checked
interpreter. `scripts/candidate_budget_cli.py` is an unconditional fail-only
compatibility stub; no caller-controlled environment marker can authorize direct
execution. `scripts/candidate-budget config --json` reports the effective budget.

## Temporal fallback and spec skeleton

The deterministic fallback accepts only explicitly supported grammar. Alongside
ISO ranges and `past N days`, it accepts month-name ranges such as:

```text
August 18-23, 2026
from August 18 to August 23, 2026
```

Impossible dates, reversed ranges, multiple temporal constraints, and fuzzy
forms remain fail-closed. The schema is
`schemas/research-workflow/research-spec-v1.json`. Generate a valid starting
spec without database/network access with:

```bash
scripts/fsearch_smart --spec-skeleton
scripts/fsearch_smart 'Research objective' --spec-skeleton
```

## Extraction first-byte retry

`FIRECRAWL_EXTRACTION_FIRST_BYTE_TIMEOUT_RETRIES` is a dedicated retry budget
for `first_byte_timeout`; default `1`. It is independent of
`FIRECRAWL_EXTRACTION_TRANSIENT_RETRIES`. Provider-operation timeouts are not
made retryable by this setting. Every retry is still bounded by the existing
overall candidate wall-clock deadline, and a timed-out child process is reaped
before another provider operation starts.

One durable extraction candidate outcome is produced for the bounded operation.
Transport telemetry may contain a bounded `provider_sub_attempts` array so an
operator can distinguish timeout→success from exhausted first-byte timeout.

## Corpus identity domains and passage semantics

`scripts/finspect inspect` and `scripts/finspect passages` use the PostgreSQL
identity crosswalk for promotion subject, search candidate, extraction attempt,
source, snapshot, document, derivation, and chunk UUIDs. Existing
`search_response` inspection remains supported as its pre-existing history
identity and is not incorrectly forced through the corpus crosswalk.

For passage retrieval, existing directly supported corpus identities retain the
legacy bounded query/cursor path. A promotion-subject UUID falls back to its
**exact persisted `run_asset_promotion_subjects.snapshot_id`**; it never selects
the first UUID from a broader candidate lineage. Pagination remains scoped to
that exact retained snapshot. A multiple-attempt regression proves a different
snapshot for the same candidate cannot leak into subject passage results.

Identity failures are emitted as structured inspection diagnostics. Stable codes
include `not_found`, `unsupported_identity_type`, and `no_retained_passages`;
ambiguous cross-domain UUID membership is reported as unsupported rather than
assigned arbitrary precedence. These are inspection failures, not generic
persistence exceptions.

`scripts/research-db fetch-passages` remains intentionally **chunk-only**.
Positional IDs are PostgreSQL `chunks.id` UUIDs. Supplying another known identity
type returns `wrong_identity_type` with detected/expected type, related IDs, and
guidance to use `finspect passages`. No Qdrant lookup participates in explicit
PostgreSQL identity or passage authority.

## Planner zero-yield diagnostics

Issue #305 does not change production planner query construction based on the
single audited site-scoped zero-result observation. Use the offline harness to
capture a reproducible comparison:

```bash
scripts/planner-yield-diagnostic \
  --scoped-query 'topic terms site:example.test' \
  --unscoped-query 'topic terms' \
  --scoped-count 0 \
  --unscoped-count 7 \
  --planner-provenance-json '{"planner":"<name>","revision":1}'
```

The output records planner provenance, normalized query shape, site scope,
candidate counts, and the observed yield delta. It never calls a provider and
always reports `production_planner_change_authorized=false`. Production planner
tuning requires a separately demonstrated deterministic defect; provider-specific
domain hardcoding is not part of this issue.

## Review-gate regression matrix

The review-remediation regressions additionally prove:

- pre-existing search-response inspection remains valid;
- promotion-subject passage lookup uses the exact retained snapshot even when the
  same candidate has another extraction attempt/snapshot;
- unknown, ambiguous, unsupported, and no-retained-passage outcomes remain typed;
- stale soft checks cannot mask an unrelated promotion failure;
- spoofing the retired candidate-budget wrapper marker cannot bypass provenance;
- a real canonical local venv is selected and resolves the exact checkout;
- the `fsearch_smart` operator-action boundary emits exit `75` plus exact wrapper
  recovery commands, while the PostgreSQL integration proves the exact override
  resumes and seals the same run.
