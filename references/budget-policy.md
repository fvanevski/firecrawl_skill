# Versioned deterministic budget policy

`scripts/budget_policy.py` implements `budget-policy-v1`, whose checked-in
configuration is `budget-policy-v1.json`. A validated `ResearchSpec` maps to a
focused, standard, or intensive resource profile. Objective word count, topic
length, and the legacy complexity label are not policy inputs.

## Policy inputs and outputs

The deterministic tier rules inspect research archetype, risk level, question
and claim counts, freshness requirements, corroboration and contradiction
requirements, and required source minima. The selected profile supplies hard
caps for search branches, results per branch, extraction attempts and
successes, adaptive cycles, LLM calls, model input/output tokens, retrieval and
reranker candidates, evidence-packet tokens, and wall-clock duration.

Every snapshot contains the policy version, SHA-256 of the exact canonical
configuration, ResearchSpec identity and revision, run revision, normalized
semantic inputs, matched rule IDs, original policy caps, user limits, and
effective caps. `BudgetPolicy.authorize` returns machine-readable rejections;
an over-budget resource uses the stable rule ID `budget.<resource>`.

User limits may only tighten a policy cap. Unknown, negative, non-integer, or
looser values fail before acquisition with `user_limit.*` rule IDs. Tightening
an extraction-attempt or retrieval-candidate limit also tightens its dependent
success or reranker cap so the effective snapshot remains internally valid.

## Persistence and revision rules

Alembic revision `0007_budget_snapshots` adds the append-only
`research_budget_snapshots` table and a current snapshot pointer on
`research_runs`. PostgreSQL remains authoritative. A snapshot is bound by
same-run foreign keys to its immutable `research_specs` row and records
canonical content and policy-configuration hashes.

The same `(run, policy version, run revision)` may be retried only with
identical content. A changed snapshot requires either a new policy version or
an explicit current run revision. Conflicting idempotency-key reuse, stale run
revisions, mismatched spec revisions, and cross-run specs fail closed.

For a persisted explicit research run, `fsearch_smart` writes
`_research_spec.json` and `_budget.json`, then calls `research-db budget-record`
before acquisition. If authoritative persistence is enabled and that write
fails, acquisition does not start. Filesystem-only and private runs retain the
same local artifacts without creating PostgreSQL state.

## Compatibility and repair

`--complexity` remains accepted as diagnostic metadata for migration but no
longer selects resource limits. Existing numeric flags are stricter user caps;
they cannot increase policy allowances. This is an intentional compatibility
change required by FR-004.

Revision 0007 is additive and does not rewrite corpus, snapshot, derivation,
index, job, lease, or provenance rows. Before production, capture the normal
PostgreSQL/blob/Qdrant recovery boundary. An interrupted PostgreSQL migration
rolls back transactionally and may be retried. If the schema claims v7 but its
objects are absent, restore the pre-v7 PostgreSQL backup and reapply, or add a
reviewed forward-repair migration; do not hand-create partial state.
