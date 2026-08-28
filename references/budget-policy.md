<!-- @format -->

# Versioned deterministic budget policy

`scripts/budget_policy.py` implements `budget-policy-v1`, whose checked-in
configuration is `budget-policy-v1.json`. A validated `ResearchSpec` maps to a
focused, standard, or intensive resource profile. Objective word count and topic
length are not policy inputs.

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

For controller-owned research, the validated ResearchSpec, immutable budget snapshot, and SearchPlan are persisted through PostgreSQL application services before acquisition. No diagnostic file is workflow authority and the current public `fresearch` surface does not expose generated ResearchSpec/budget files or a manual adaptive-cycle override. If authoritative planning persistence or budget validation fails, acquisition does not start.

Specialist/application callers may provide tighter validated limits through supported internal/service contracts, but no caller may exceed the persisted policy cap or reconstruct generated budget parameters outside the application boundary.

## Repair

The current schema is a clean PostgreSQL-authoritative baseline. Existing
pre-baseline databases are intentionally unsupported and must be reset with
`scripts/reset-firecrawl-research`. Interrupted migrations roll back
transactionally and may be retried. Do not hand-create partial state.
