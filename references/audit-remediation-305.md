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

## Smart-run disposition

`scripts/fsearch_smart` prints `Run ID`, `Final state`, `Orchestrator outcome`,
and `Next action` for every completed invocation.

- `completed` and terminal `partial` return `0`.
- `checkpoint`, `resumable`, and `operator_action_required` return `75` and must
  continue the same run only after the printed next action is satisfied.
- `failed`, `cancelled`, an explicit error, and an unrecognized nonterminal
  result return non-zero.

A soft completion-admission candidate-budget gate returns
`operator_action_required` without sealing/index-completion advancement. Use the
canonical wrapper, never the raw Python module:

```bash
scripts/candidate-budget checks <fr_run_id>
scripts/candidate-budget override <fr_run_id> <check_uuid> <soft_limit_name> \
  --reason '<justification>' --author '<operator>'
scripts/fsearch_smart '<same objective>' --research-run-id <fr_run_id>
```

Overrides remain bound to the exact persisted check, lifecycle revision, scope,
and membership fingerprint. Hard violations cannot be overridden. A membership
change requires a new exact completion-admission decision.

`scripts/candidate-budget config --json` reports the effective candidate-budget
configuration. Direct execution of `scripts/candidate_budget_cli.py` is rejected
with guidance to use the wrapper so runtime provenance cannot be bypassed.

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

## Corpus identity domains

`scripts/finspect inspect` and `scripts/finspect passages` use a read-only
PostgreSQL crosswalk that reports the supplied `identity_type` and related
persisted IDs across:

- promotion subject;
- search candidate;
- extraction attempt;
- source;
- snapshot;
- document;
- derivation; and
- chunk.

The resolver follows explicit PostgreSQL relationships and rejects ambiguous
UUID membership rather than guessing from value equality. Promotion-subject and
candidate relationships remain run-scoped; no Qdrant lookup participates in
identity resolution.

`scripts/research-db fetch-passages` is intentionally **chunk-only**. Positional
IDs are PostgreSQL `chunks.id` UUIDs. Supplying another known identity type is a
`wrong_identity_type` error that reports the detected type, related IDs, and
guidance to use `finspect passages` for higher-level identities. Existing valid
chunk-ID output is unchanged.

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

The output records planner provenance, normalized query shape, site-scope
metadata, candidate counts, and the observed yield delta. It never calls a
provider and always reports `production_planner_change_authorized=false`.
Production planner tuning requires a separately demonstrated deterministic
planner defect; provider-specific domain hardcoding is not part of this issue.
