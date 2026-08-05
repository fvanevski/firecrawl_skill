# Indexing checkpoint and terminal-decision contract

Issue #210 establishes a PostgreSQL-authoritative checkpoint boundary for the
`indexing -> coverage_review` transition and a structurally atomic boundary for
terminal lifecycle decisions. Qdrant remains a rebuildable projection and is
never lifecycle, provenance, census, or exact-membership authority.

## Public checkpoint CLI

```text
frun resume <fr_id> [--batch-size N] [--max-batches N]
  [--deadline-seconds N] [--initial-backoff-seconds N]
  [--max-backoff-seconds N]
```

For a run in `indexing`, the command reloads or creates the active checkpoint for
its current PostgreSQL lifecycle revision, drains only the sealed chunk
membership, appends each authoritative census observation, and performs a fresh
row-locked compare-and-swap before advancing to `coverage_review`.

For a run that remains at the exact `coverage_review` revision produced by a
completed checkpoint, the command performs a **read-only replay**. It verifies:

- the sealed PostgreSQL membership and membership digest;
- the persisted index fingerprint and unique index definition;
- a fresh exact PostgreSQL job census and manifest count;
- the completed checkpoint lifecycle revision; and
- the immutable transition completion payload.

A successful replay returns the original result without invoking the embedder,
writing an observation, or creating another lifecycle transition. A run that has
moved beyond that exact revision is not an unchanged retry and fails closed.

Exit code `0` means the exact sealed census completed and the guarded transition
was advanced or its immutable result was replayed. Exit code `75` means bounded
work remains recoverable and the run stays in `indexing`. Exit code `130` means
cancellation was observed and the checkpoint remains resumable. Exit code `1`
is fail-closed for invalid setup, changed membership, fingerprint or revision,
malformed transition evidence, or irrecoverable census classes.

## Atomic terminal decisions

Migration file `0039_indexing_checkpoints_terminal_guard.py` has Alembic revision
`0039_index_checkpoint_guard`. It binds every **new** terminal decision to one
terminal transition by all of the following:

1. the same run-scoped idempotency key;
2. the same PostgreSQL transaction identity;
3. `decision.run_revision + 1 == transition.lifecycle_revision`; and
4. a deterministic outcome-to-state mapping:
   `sufficient -> completed`, `partial|blocked -> partial`,
   `failed -> failed`, and `cancelled -> cancelled`.

The transition-side trigger rejects missing, historical, cross-transaction, or
semantically mismatched decisions. A deferred decision-side constraint trigger
rejects an orphan decision when its transaction attempts to commit. Thus neither
ledger can be committed independently for a new terminal command.

`TerminalDecisionService.record()` is retained only as a fail-closed compatibility
surface. It performs no insert and directs callers to
`ResearchRunService.commit_terminal_decision()` or the guarded lifecycle helpers,
which insert the structured decision and transition in one unit of work.

The package-level `research_store.ResearchRunService` symbol and the container
builder continue to resolve to `GuardedResearchRunService`. The base class in
`research_store.run_service` remains an internal compatibility implementation,
not an alternate terminal-write entry point.

## Historical provenance and migration behavior

The migration explicitly updates only terminal-decision rows that exist at the
0038-to-0039 migration boundary with:

```json
{
  "reason_code": "legacy_unstructured",
  "state_census": {
    "schema_version": "terminal-state-census-v1",
    "available": false,
    "reason": "legacy_unstructured"
  }
}
```

Because the preexisting terminal-decision ledger is append-only, migration 0039
disables that table's append-only trigger only around this controlled historical
backfill and re-enables it before installing the new constraints and terminal
command triggers. The entire upgrade remains one PostgreSQL transaction, so no
externally visible interval permits unguarded decision mutation.

Those historical rows have no transaction identity and cannot authorize a new
terminal transition. The `reason_code` and `state_census` columns have **no
legacy defaults** after migration; every future authoritative writer must supply
structured values. The migration does not infer historical membership, census
state, reason codes, or transaction provenance.

Because the PR is not yet merged and migrations are forward-only, a disposable
development database that previously ran an earlier candidate version of 0039
must be recreated or restored to the pre-v39 recovery boundary before testing
this revised migration. Production upgrade scope remains 0038 to the final 0039.

## Wrapper compatibility and fail-closed routing

Wrapper operation names, invocation identifiers, input object shape, and
terminal-run status are validated before checkpoint finalization. Invalid
commands therefore cannot advance an indexing run, complete a checkpoint, append
a transition, or create an invocation before being rejected. Valid `fsearch` and
`fscrape` paths continue to use the existing
`WorkflowOperationService` allowlist and lifecycle rules.

## Validation matrix

| Contract | Production seam | Regression coverage |
|---|---|---|
| Explicit historical backfill; no future legacy defaults | 0038 -> 0039 Alembic upgrade | `test_v39_backfills_only_preexisting_decisions_and_removes_legacy_defaults` |
| No orphan terminal decision | deferred PostgreSQL constraint trigger | `test_orphan_terminal_decision_cannot_commit` |
| Decision and transition agree semantically | transition-side PostgreSQL trigger | `test_semantically_mismatched_terminal_command_rolls_back` |
| One transaction identity for both ledgers | guarded terminal command | `test_guarded_terminal_command_persists_one_atomic_semantic_pair` |
| Concurrent identical terminal retries reuse one pair | guarded idempotent insert/CAS | `test_concurrent_identical_terminal_commands_reuse_one_atomic_pair` |
| Standalone writer fails before SQL | `TerminalDecisionService.record` | `test_standalone_terminal_decision_service_fails_before_writing` |
| Public replay is idempotent and read-only | `resume_index_checkpoint.main` | `test_public_resume_replays_completed_checkpoint_without_new_transition` |
| Invalid wrapper operation has zero mutation | built workflow operation service | `test_invalid_wrapper_operation_is_rejected_without_any_mutation` |
| Concurrent checkpoint finalization has one transition | row-locked checkpoint finalizer | existing checkpoint terminal integration test |
| Recoverable work remains nonterminal and bounded | drain/checkpoint stage | existing checkpoint contract and integration tests |

The dedicated `Index checkpoint` workflow executes this matrix against disposable
PostgreSQL and Qdrant services on Python 3.11 and 3.12. The general CI workflow
independently runs Ruff checking, Ruff format verification, the full `scripts/`
test suite on both Python versions, release invariants, and strict campaign
contract tests.

## Non-goals

This change does not make Qdrant authoritative, infer historical provenance,
alter evidence or synthesis completion gates, permit unbounded waits, change
terminal state names, or absorb later audit-campaign issues. Migration downgrade
remains unsupported; recovery uses a forward repair or the documented pre-v39
PostgreSQL recovery boundary.
