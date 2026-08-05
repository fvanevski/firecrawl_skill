# Staged asset promotion and sealed completion membership

Issue #211 adds a PostgreSQL-authoritative promotion ledger and an exact,
hash-addressed completion-membership boundary in Alembic revision
`0040_asset_promotion_membership`. Qdrant remains a rebuildable vector
projection; it is never lifecycle, promotion, provenance, or exact-membership
authority.

## Promotion stages

New authoritative work uses these persisted stages:

```text
discovered
  -> selected_for_extraction
  -> extracted
  -> retained
  -> evidence_eligible
  -> completion_critical
```

Any non-rejected stage may transition to `rejected` where permitted by the
migration's state-machine guard. All other stage skips and reversals fail
closed.

Each stage event records the run, subject, prior and next stage, trigger-managed
stage revision, actor, actor identifier, policy version, run lifecycle revision,
reason code, optional reason, timestamp, and PostgreSQL transaction identity.
The event table is append-only.

A persisted search candidate establishes `discovered`. Creating an extraction
attempt establishes `selected_for_extraction`, but its provisional row is not
success evidence. Only a finalized successful extraction with `end_time` and at
least one persisted raw or normalized blob digest establishes `extracted`.
Failed, partial, and cancelled final results remain selected unless explicitly
rejected. Once an attempt supports an `extracted` event, the status, completion
time, and output digests that support that immutable provenance cannot be
rewritten.

A legacy or direct authoritative ingestion path may persist the content-addressed
snapshot in the same transaction that links it to the run. In that case, the
retention trigger accepts the linked snapshot itself as output evidence only when
the linked extraction attempt is finalized as successful and the snapshot has a
content digest, blob URI, and positive byte length. It then records the explicit
`extracted` event before `retained`; it does not skip or synthesize either stage.

Linking the resulting snapshot to the run establishes `retained`. Extraction
success and retention do **not** implicitly establish either
`evidence_eligible` or `completion_critical`.

Generic run-asset links that have no candidate/extraction lineage begin at
`retained` with `direct_retention` provenance. This records the known retention
fact while explicitly declining to invent discovery or extraction history.

## Indexing admission policy

Candidate-ranking policy is outside issue #211. At the indexing boundary, the
current compatibility policy explicitly advances each retained run asset through
`evidence_eligible` and then `completion_critical`. Each legal transition commits
separately, so a cancellation or failure resumes from the last durable stage.
The policy and reason are recorded in the promotion ledger and can be replaced by
a later evidence-selection policy without changing the membership contract.

## Exact completion-membership seal

Only subjects currently at `completion_critical` contribute to a new indexing
barrier. While holding the run row lock, the service:

1. reads completion-critical PostgreSQL subjects in deterministic order;
2. resolves their exact configured PostgreSQL chunk derivations;
3. persists each member and its ordered chunk IDs;
4. records the asset count and the de-duplicated exact chunk count; and
5. hashes the canonical member representation with SHA-256.

PostgreSQL does not trust values calculated by the service. Deferred constraint
triggers recomputes and verifies every member hash, the aggregate membership
hash, the persisted asset count, the distinct chunk count, and contiguous member
ordering before commit. Member chunk arrays must be non-null, sorted, and free
of duplicates. A syntactically valid but non-addressing hash or inconsistent
count therefore aborts the sealing transaction.

A new indexing checkpoint must bind to the active seal. Its independently hashed
chunk membership must exactly equal the seal's persisted chunk IDs and expected
chunk count. Completion evidence records both checkpoint and asset-membership
identities, counts, and hashes under the transition ledger's
`validation_result.completion` object.

Sealing is idempotent when the persisted member set is unchanged. Promotion into
or out of `completion_critical` while a seal is active fails closed. A caller must
explicitly reopen the seal with the current lifecycle revision, which also
invalidates any active checkpoint, then apply the membership change and reseal.
All seal, promotion, and checkpoint writers acquire the run lock before subject
locks, so concurrent promotion and sealing have one deterministic order.

A late `retained` or `evidence_eligible` asset may still be persisted after a
seal, but it remains outside that exact barrier until an explicit reopen,
completion-critical promotion, and reseal. Removing or changing a sealed member's
run-asset identity is rejected.

## Historical compatibility

Revision 0040 does not backfill promotion subjects or events for pre-existing
`research_run_assets`. Compatibility reads report those rows as:

```text
current_stage = unknown
provenance = legacy_unstructured
```

No earlier stage, actor, policy, reason, or timestamp is inferred. Existing
pre-0040 active checkpoints remain readable and resumable through the prior
checkpoint contract. Creating a new sealed checkpoint for a run containing an
unknown historical asset fails with an explicit compatibility error until an
evidence-bearing forward repair supplies authoritative promotion provenance.

The migration is additive and forward-only. `downgrade()` fails closed; recovery
requires a forward repair or restoration from a PostgreSQL backup.

## Required regression coverage

The issue-specific suite exercises the production extraction service rather
than a direct parser or constant seam. It covers successful, failed, partial,
and cancelled completion; immutable successful provenance; invalid stage skips;
PostgreSQL rejection of false member/seal hashes and duplicate chunks;
idempotent sealing; exact checkpoint binding; explicit reopen/reseal CAS;
deterministic race orderings; interruption recovery; and pre-0040 compatibility.
