# Curated direct-acquisition lifecycle

Issue #212 adds an explicit run-mode contract for direct Firecrawl work while
preserving PostgreSQL as the authority for lifecycle, invocation, provenance,
asset membership, and completion. `BLOB_ROOT` remains the immutable payload
store. Qdrant remains a rebuildable projection.

## Run modes

`frun start` now accepts an additive `--run-mode autonomous|curated` option.
The existing `--mode agent_led|autonomous_local|deterministic_debug` option
continues to select execution policy and has not changed meaning.

The declared run mode is stored in `research_runs.metadata.run_mode`. New
`frun` runs always persist either `autonomous` or `curated`. Historical rows
without the field are reported as `legacy_unspecified`; no migration infers or
backfills historical mode or provenance.

## Explicit direct-acquisition commands

A direct wrapper invocation is legal only while the run is in `acquiring`.
Neither `fsearch` nor `fscrape` moves lifecycle state when it begins or
completes. The existing `--research-run` wrapper argument is unchanged; the
run must now be prepared explicitly before either wrapper is invoked.

```text
frun start "objective" --run-mode curated
frun prepare <fr_id>
fsearch ... --research-run <fr_id>
fscrape ... --research-run <fr_id>
frun retain <fr_id> <promotion_subject_id>
frun reject <fr_id> <promotion_subject_id> --reason "..."
frun seal-acquisition <fr_id>
frun resume <fr_id>
frun finish <fr_id> --outcome satisfied
```

`frun prepare` is the explicit, idempotent transition from `created`,
`planning`, or `corpus_review` to `acquiring`. An invalid direct call fails
before provider execution and reports the current state together with the
`frun prepare` remediation.

Regression and integration fixtures follow the same contract: creating a run
does not prepare it. Tests that exercise a real direct wrapper must invoke the
explicit preparation boundary instead of relying on an implicit wrapper
transition.

Each direct invocation locks the run row with `FOR SHARE` and records the exact
start state and lifecycle revision in PostgreSQL invocation metadata and the
append-only `invocation_started` event before the lock is released. Historical
invocations are not rewritten.

## Curated membership and sealing

`retain`, `reject`, and `seal-acquisition` are available only for runs declared
`curated`. Retain/reject reuse the staged asset-promotion authority introduced
by issue #211. `seal-acquisition` is the only curated command that advances the
run from `acquiring` through `extracting` to `indexing`; it then promotes the
retained subjects through evidence eligibility, seals exact completion
membership, and binds the PostgreSQL chunk set used by indexing.

The command is idempotent. Repeating it reuses the existing lifecycle
transitions and membership seal. It does not discover candidates, invoke an
autonomous planner, or perform smart expansion.

## Resume and finish

For a curated run, `frun resume` is mode-aware:

- before acquisition, it reports `frun prepare` as the next action;
- during acquisition, it reports `frun seal-acquisition`;
- during indexing, it resumes the bounded checkpoint workflow;
- after indexing, it reports the explicit finish command;
- after terminal completion, it is an observable no-op.

`frun finish` uses the existing checkpoint-guarded finish boundary. It never
invokes autonomous search expansion. Repeating finish after a terminal result
returns the same authoritative status without new transitions.

## Schema and compatibility impact

No DDL migration is required. Run mode and direct-invocation lifecycle state
are additive keys in existing PostgreSQL JSONB metadata, while the existing
non-null `research_invocations.lifecycle_revision` column remains the numeric
revision authority. Compatibility reads return `legacy_unspecified` or a
missing lifecycle-state key for historical records instead of fabricating
history.
