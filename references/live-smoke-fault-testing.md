# Canonical live smoke/fault validation

`scripts/live_validate.py` is the **only current live smoke/fault validation
authority** for the Firecrawl Research Skill. It exercises the installed public
CLI surfaces against a clean committed checkout and emits one typed
`live-validation-v2` manifest.

`scripts/live_fault_validate.py` is a deprecated compatibility entry point. It
imports and invokes the canonical validator and owns no policy, case matrix,
service lifecycle, or evidence semantics of its own.

The deprecated `scripts/fsearch_smart` name remains an exact delegate to
`scripts/fresearch run`. Live validation must not restore its retired
`--dry-run`, `--stop-after-state`, `--research-run-id`, or
`--max-adaptive-cycles` options. Those spellings are exercised only as negative
compatibility assertions.

## Evidence model

Every case records these independent fields:

| Field | Meaning |
|---|---|
| `contract_result` | Whether the observed CLI/service behavior matched the case's current typed contract. An expected fail-closed result can therefore be `PASS`. |
| `capability_result` | Whether a case designated as a positive capability actually succeeded. Negative/fault cases use `NOT_EVALUATED`. |
| `observed_disposition` | The normalized public disposition actually observed (`terminal_completed`, `terminal_partial`, `preflight_rejection`, `extraction_failure`, and so on). |

A valid failure is never promoted into capability success. In particular, a
typed `authoritative-fscrape-error-v1` extraction failure can satisfy its fault
contract while `capability_result` remains `NOT_EVALUATED`.

For `fresearch`, contract conformance requires validation against the current
repository-owned `workflow-directive-v2` / `research-result-v3` JSON schema in
addition to the disposition/process-exit mapping. A matching `schema_version`
string alone is not contract evidence. A positive research capability passes
only when the validated public result is `research-result-v3`,
`disposition=terminal_completed`, and `objective_satisfied=true`.

The aggregate manifest also reports:

- declared/executed matrix counts;
- plumbing operation count;
- not-run and failed cases;
- successful contract and capability counts;
- Firecrawl provider-operation count and bounded call metadata;
- `quality_metrics` / `quality_result` for positive runs;
- validator-owned run cleanup;
- monitored temporary-storage cleanliness;
- exact implementation Git HEAD; and
- final `host_evidence`.

For persistent-service profiles, `host_evidence=PASS` requires all required
contracts, every designated positive capability, required corpus/blob/index/
Qdrant integrity, validator-owned run cleanup, and temporary-storage purity.
A contract-only failure-path PASS cannot masquerade as a complete live
capability PASS.

There is no automatic case retry. `retry_policy.max_attempts_per_case=1`; a
rerun is a new validation campaign with a new evidence identity.

## Exact source identity

The validator records the checked-out 40-character Git HEAD and fails when
tracked source is dirty. For exact-head PR evidence, additionally bind the
expected candidate explicitly:

```bash
scripts/live_validate.py \
  --profile focused \
  --expected-head-sha '<40-character-PR-head>' \
  --artifact-root ./validation-artifacts
```

`FIRECRAWL_VALIDATION_HEAD_SHA` is the environment equivalent. Untracked
presentation artifacts are not source identity; tracked/indexed drift is.

## Persistent-service profiles

The default operation cap is profile-specific:

| Profile | Default/max Firecrawl operations | Purpose |
|---|---:|---|
| `focused` | 40 | Current public controller smoke plus required fail-closed compatibility/fault assertions |
| `failure-path` | 20 | Failure typing and one real Valkey-loss capability path |
| `full` | 100 | Broader current controller plus direct search/scrape capability coverage |
| `destructive` | 10 | Separate disposable-service fault profile; it does not use persistent destructive mutation |

Examples:

```bash
scripts/live_validate.py --profile focused
scripts/live_validate.py --profile failure-path
scripts/live_validate.py --profile full
```

The persistent profiles bind the live environment through the public repository
wrappers and keep Firecrawl provider activity behind the validator's
operation-counting proxy. They include the following current matrix:

1. exact implementation identity;
2. authoritative PostgreSQL `ingest-ready`;
3. compatible active Qdrant alias;
4. installed Firecrawl CLI;
5. negative rejection of all four retired `fsearch_smart` options with **zero**
   provider activity;
6. unprepared direct acquisition rejected before provider activity;
7. prepared direct acquisition with an unreachable Firecrawl endpoint producing
   a current typed extraction failure: either the exception envelope
   `authoritative-fscrape-error-v1` at `failure_stage=extraction`, or the normal
   `authoritative-fscrape-v1` batch contract with `status=failed` and a failed
   item; both require process exit `5`; 
8. one or more positive current public capabilities, depending on profile.

`focused` uses `scripts/fresearch run` as its positive normal-agent surface.
`failure-path` additionally proves direct acquisition can complete with Valkey
unavailable. `full` expands the normal-agent topics and adds current direct
`fsearch` / `fscrape` coverage.

The validator does not use retired smart-controller options as a positive path.

## Validator-owned run cleanup

The validator snapshots all pre-existing `research_runs.external_run_id` values
before matrix execution. Every validator-created objective is tagged with the
current campaign/case identity. Explicit returned run IDs are accepted only
after PostgreSQL readback proves that exact tagged objective belongs to that
non-baseline ID. If a command fails before returning its run ID, recovery
discovery is restricted to that exact tagged objective; ambiguous matches are
reported as failure and are **not** claimed for cleanup. The validator never
infers ownership from an arbitrary "new since baseline" set.

At finalization:

- pre-existing runs and concurrent unrelated runs are excluded and never mutated;
- validator-owned terminal runs are preserved as terminal provenance;
- every validator-owned nonterminal run is terminalized through
  `scripts/frun cancel <fr_id> --reason ...`;
- cleanup is re-read from PostgreSQL;
- cancellation timeouts/exceptions are recorded as machine-readable cleanup
  failures rather than escaping finalization;
- unexpected validator execution errors still enter the same discovery,
  cleanup, and manifest-finalization path; and
- any cleanup/readback failure makes aggregate host evidence fail.

`--keep-runs` is an operator diagnostic override only. It records cleanup as
`NOT_RUN`; therefore that invocation cannot produce clean aggregate
`host_evidence=PASS`.

## Disposable destructive fault profile

Persistent destructive fault execution is forbidden. Do **not** point schema
mutation, datastore corruption, queue destruction, or equivalent tests at the
persistent PostgreSQL/Qdrant/Valkey services.

The only destructive profile is:

```bash
scripts/live_validate.py \
  --profile destructive \
  --expected-head-sha '<40-character-PR-head>' \
  --disposable-namespace fc359 \
  --disposable-pg-port 55436 \
  --disposable-qdrant-port 55437 \
  --artifact-root ./validation-artifacts
```

The profile delegates service lifecycle exclusively to
`scripts/disposable-test-services`.

Safety sequence:

1. Exercise the helper's protected-port admission with its non-mutating `env`
   command for both known persistent datastore ports: PostgreSQL `55432` and
   Qdrant `6333`. The helper must reject both. The validator never attempts
   destructive `up` against a persistent-service port, even as a fault test.
2. Start a fresh repository-owned disposable namespace on loopback-only,
   non-protected ports.
3. Positively validate the helper's `firecrawl-disposable-services-v1`
   namespace, database name, PostgreSQL port, Qdrant port, and reset-authority
   fields.
4. Migrate the disposable PostgreSQL database and require `ingest-ready`.
5. Inject one bounded schema fault **only after** the disposable identity is
   proven: rename `research_runs` to `research_runs_faulted`.
6. Require `research-db ingest-ready` to fail closed.
7. Tear down the faulted namespace through the helper and require teardown to
   succeed before any recovery lifecycle begins.
8. Recreate the namespace from fresh disposable services only after successful
   teardown, migrate, and require `ingest-ready` to pass.
9. Tear down again through the helper and remove the validator-owned temporary
   blob root. Helper startup timeout/error paths remain teardown-reserved so a
   partial setup cannot silently escape cleanup.

There is no direct persistent-service reset, no use of
`scripts/reset-firecrawl-research`, no manual Docker cleanup path, and no
faulted datastore reuse as "recovery." Helper teardown is the lifecycle cleanup
authority.

## Artifact output

Without `--artifact-root`, the manifest is printed to stdout.

With `--artifact-root`, the validator creates exactly one campaign directory:

```text
<artifact-root>/<campaign-id>/
  manifest.json
  report.md
```

These files are final evidence outputs only. They are never runtime inputs.
Runtime temporary files remain under the validator-owned temporary root and are
required to be clean after each public case. Deterministic tokenizer cache is
routed to a separate validator-owned cache directory so library cache population
cannot be mistaken for retained acquisition staging.

## Interpretation

A valid issue/PR evidence packet should keep these classes separate:

```text
repository-deterministic Verify
persistent host/live smoke/fault evidence
disposable destructive-fault evidence
semantic PR review
```

For issue #359 specifically, the persistent profile and the destructive
disposable profile are complementary. Neither substitutes for repository
deterministic validation or semantic review, and a PASS from one profile is not
silently reclassified as a PASS from another.
