# Indexing checkpoint resume contract

Issue #210 adds a PostgreSQL-authoritative checkpoint boundary for the
`indexing -> coverage_review` transition. Qdrant remains a rebuildable
projection and is never used as lifecycle, provenance, or exact-membership
authority.

## Public CLI

```text
frun resume <fr_id> [--batch-size N] [--max-batches N]
  [--deadline-seconds N] [--initial-backoff-seconds N]
  [--max-backoff-seconds N]
```

The command reloads or creates the active checkpoint for the run's current
PostgreSQL lifecycle revision, drains only its sealed chunk membership, appends
each authoritative census observation, and performs a fresh row-locked
compare-and-swap before advancing to `coverage_review`. Repeating the command
with unchanged state is idempotent.

Exit code `0` means the exact sealed census completed and the guarded transition
was advanced or replayed. Exit code `75` means bounded work remains recoverable
and the run stays in `indexing`. Exit code `130` means cancellation was observed
and the checkpoint remains resumable. Exit code `1` is fail-closed for invalid
setup, changed membership/fingerprint/revision, or irrecoverable census classes.

## Migration and compatibility

Migration `0039_indexing_checkpoints_terminal_guard` is additive and
forward-only. It creates checkpoint and append-only observation ledgers, adds
structured terminal-decision fields, and requires each new terminal lifecycle
transition to have a same-transaction decision row under the same run-scoped
idempotency key. Existing terminal-decision rows receive explicit
`legacy_unstructured` markers; the migration does not infer historical
membership, census state, reason codes, or provenance. Existing nonterminal CLI
and schema contracts are unchanged.
