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

A candidate identifies a discovery target, not one permanently mutable asset.
The first extraction attempt consumes the candidate's initial `discovered`
subject. If the same candidate is extracted again after that subject has left
`discovered`, PostgreSQL creates a distinct promotion subject owned by the new
extraction attempt. A later successful snapshot therefore receives a new subject
and event chain; the prior subject, snapshot identity, and append-only events are
never rebound or rewritten. Multiple retained snapshots from one candidate remain
separately queryable and may be admitted or rejected independently.

A legacy or direct authoritative ingestion path may persist the content-addressed
snapshot in the same transaction that links it to the run. In that case, the
retention trigger accepts the linked snapshot itself as output evidence only when
the linked extraction attempt is finalized as successful and the snapshot has a
content digest, blob URI, and positive byte length. It resolves the distinct
promotion subject owned by that exact attempt, then records the explicit
`extracted` event before `retained`; it does not skip or synthesize either stage.

Linking the resulting snapshot to the run establishes `retained`. Extraction
success and retention do **not** implicitly establish either
`evidence_eligible` or `completion_critical`.

Generic run-asset links that have no candidate/extraction lineage begin at
`retained` with `direct_retention` provenance. This records the known retention
fact while explicitly declining to invent discovery or extraction history.

## Canonical stage-specific read semantics

Issue #217 (RC-13) reuses this ledger rather than creating a second selection or
promotion state machine. `AssetPromotionService.list_assets()` exposes the
following stage-specific read fields for authoritative subjects:

- `selected_for_extraction` — the subject has reached
  `selected_for_extraction`;
- `extraction_succeeded` — the subject has reached the persisted ARC-05
  `extracted` stage;
- `retained` — the subject has reached `retained`;
- `evidence_eligible` — the subject has reached `evidence_eligible`; and
- `completion_critical` — the subject has reached `completion_critical`.

These fields are derived only from the subject's current PostgreSQL stage and
its append-only promotion events. A later stage therefore preserves the fact
that earlier stages were reached without inferring anything that is absent from
the ledger. `extraction_succeeded` is the externally explicit read name for the
persisted `extracted` stage; it does not reinterpret a provisional extraction
attempt or a generic boolean selection field as success.

Historical compatibility remains conservative. For a pre-0040 run asset with
`current_stage = unknown` and `provenance = legacy_unstructured`, each of the
five stage-specific fields is `None`, not `False`. `False` would assert that a
stage was definitely never reached, which cannot be proven for history that was
never recorded.

Legacy storage/API fields named only `selected` remain compatibility fields and
must not be treated as lifecycle authority. New reads use a stage-specific name:
retrieval traces expose `selected_for_retrieval`, while extraction and asset
lifecycle consumers use the five ARC-05 fields above. In particular, a bare
`selected` value must not be used to infer extraction success, retention,
evidence eligibility, or completion membership.

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
triggers recompute and verify every member hash, the aggregate membership hash,
the persisted asset count, the distinct chunk count, and contiguous member
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
and cancelled completion; immutable successful provenance; repeated extraction
of one candidate into distinct subjects and snapshots; invalid stage skips;
PostgreSQL rejection of false member/seal hashes and duplicate chunks;
idempotent sealing; exact checkpoint binding; explicit reopen/reseal CAS;
deterministic race orderings; interruption recovery; and pre-0040 compatibility.

Issue #217 additionally verifies the canonical stage-specific read fields at
successive promotion stages and verifies that pre-0040 unknown history keeps
all five fields unknown rather than fabricating negative assertions.
