# Candidate ranking and corpus-budget policy

Issue #215 adds a PostgreSQL-authoritative policy layer between search-candidate persistence, extraction, and completion-critical asset membership. The policy is intentionally separate from Qdrant: vector storage remains a rebuildable projection and is never consulted for candidate-policy authority or exact membership.

## Ranking

Every non-empty authoritative `fsearch` response is structurally classified and scored before candidate selection. The canonical classifier is `candidate_ranking.classify_url` and supports `article`, `live_blog`, `official_release`, `topic_hub`, `home_page`, `reference_page`, `search_page`, and `unknown`.

The score records provider ordinal, structural penalty, freshness status/penalty, persisted duplicate-group membership, expected-size evidence when it actually exists in persisted provider metadata, the total score, and a human-readable rationale. Wikipedia article URLs are treated as reference pages for this narrow-objective policy: they receive the configurable reference-page penalty but are not universally invalid. AP hub URLs and home pages receive their corresponding generic-page penalties.

Malformed, scheme-less, and non-HTTP URLs classify as `unknown`; they are not silently promoted to articles. Freshness is `satisfied` through the configured stale threshold and `unsatisfied` only after it. An explicit `fsearch --tbs qdr:h|d|w|m|y` window narrows the freshness threshold for that invocation. Without an explicit window, `FIRECRAWL_RANK_STALE_AFTER_DAYS` controls the threshold (default 365 days).

Ranking output is immutable audit evidence in `candidate_rankings`. Each row is tied to the run, search response, fsearch invocation, and persisted candidate and records whether the candidate was selected or rejected. No historical ranking rows are inferred during migration.

## Budget phases and fail-closed behavior

Candidate/corpus limits are evaluated in three persisted phases. Canonical controller planning additionally snapshots the configured `CandidateBudget` beside the immutable planning resource budget and persists the overlapping planned-acquisition extraction-attempt cap as the stricter of those two hard limits. Planned scheduling and restart consume that persisted cap before constructing extraction work, and later candidate-policy checks for the same planned run reuse the persisted candidate budget rather than re-reading process-local configuration.

1. `pre_extraction`: candidate composition and projected extraction attempts are checked before constructing or invoking direct-scrape transport.
2. `post_extraction`: actual PostgreSQL-retained bytes, chunks, per-asset contribution, generic-page share, and extraction-attempt counts are checked after successful extraction.
3. `completion_admission`: the exact evidence-eligible/completion-critical PostgreSQL subject set is checked while the run lifecycle row is locked, before admission to completion-critical membership.

Hard limits are never overridable. The default hard limits are candidate count, total retained bytes, total chunks, and exploratory extraction attempts. Soft limits are generic-page share and per-asset chunk contribution. A soft violation is **not accepted** until a matching override exists.

The completion-membership seal recomputes the locked final set and requires it to match an accepted `completion_admission` check. Direct promotion to `completion_critical` has the same gate, so callers cannot bypass the policy by avoiding the normal preparation loop. The membership and policy checks remain PostgreSQL-authoritative and use the existing lifecycle compare-and-swap lock; Qdrant, presentation exports, provisional synthesis, and inferred history cannot satisfy the gate.

## Exact soft overrides

A soft override is immutable and belongs to one `corpus_budget_checks` row and one violated soft limit. It requires an explicit non-empty reason and author. Attempts to override a hard limit or a limit that was not violated fail closed.

Budget-check fingerprints contain the authoritative candidate/asset scope and configured limits, but not a transient fsearch invocation identifier. This permits a deliberate retry of the *same* persisted candidate scope after an operator records an override. If the candidate or asset scope changes, the fingerprint changes and the previous override does not authorize the new scope.

Inspect checks:

```bash
scripts/candidate-budget checks <fr_id>
```

Record an override:

```bash
scripts/candidate-budget override <fr_id> <budget-check-uuid> \
  max_generic_page_share \
  --author "<operator identity>" \
  --reason "<specific justification for this exact scope>"
```

Show effective environment-configured limits:

```bash
scripts/candidate-budget config --json
```

## Configuration

Candidate-budget environment variables:

- `FIRECRAWL_BUDGET_MAX_CANDIDATES`
- `FIRECRAWL_BUDGET_MAX_BYTES`
- `FIRECRAWL_BUDGET_MAX_CHUNKS`
- `FIRECRAWL_BUDGET_MAX_PER_ASSET_CHUNKS`
- `FIRECRAWL_BUDGET_MAX_GENERIC_PAGE_SHARE`
- `FIRECRAWL_BUDGET_MAX_EXTRACTION_ATTEMPTS`

Ranking environment variables:

- `FIRECRAWL_RANK_GENERIC_PAGE_PENALTY`
- `FIRECRAWL_RANK_HOME_PAGE_PENALTY`
- `FIRECRAWL_RANK_REFERENCE_PAGE_PENALTY`
- `FIRECRAWL_RANK_SEARCH_PAGE_PENALTY`
- `FIRECRAWL_RANK_UNKNOWN_PAGE_PENALTY`
- `FIRECRAWL_RANK_STALE_DATE_PENALTY`
- `FIRECRAWL_RANK_DUPLICATION_PENALTY`
- `FIRECRAWL_RANK_EXTREME_SIZE_PENALTY`
- `FIRECRAWL_RANK_LARGE_CHAR_THRESHOLD`
- `FIRECRAWL_RANK_SMALL_CHAR_THRESHOLD`
- `FIRECRAWL_RANK_STALE_AFTER_DAYS`

Standalone policy services validate these values at service construction. Canonical controller planning validates and snapshots the configured candidate budget once with the run's persisted planning tuple; resume and completion reuse that exact run authority. A planned run with a malformed or incomplete persisted candidate-budget authority fails closed instead of substituting current environment defaults. Invalid values therefore fail before planned acquisition/search transport.

## Migration and compatibility

Migration `0042_candidate_ranking_budgets` is additive and forward-only. It creates append-only `candidate_rankings`, `corpus_budget_checks`, and `budget_override_justifications` tables with relational foreign keys to authoritative PostgreSQL identities. Existing historical candidates, assets, and runs are left unchanged: the migration does not synthesize prior ranking decisions, budget checks, overrides, or provenance.

Runs with historical assets whose promotion stage is unknown continue to fail closed under the existing issue #211 compatibility policy. Recovery from migration 0042 is a forward repair or PostgreSQL restore, not a destructive downgrade.
