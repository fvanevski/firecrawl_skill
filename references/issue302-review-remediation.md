# Issue #302 / PR #303 review remediation

This note records the narrow follow-up changes made after independent review of PR #303. It is implementation evidence and operator guidance; issue #302 remains the acceptance authority.

## Run BLOB ownership

`frun verify` treats PostgreSQL as the authority for run ownership and `BLOB_ROOT` as the authority for immutable bytes. A snapshot is run-owned when either of the following authoritative persistence paths establishes the relationship:

- `research_run_assets(run_id, snapshot_id, role)` links the snapshot directly to the run; or
- `asset_snapshots.extraction_attempt_id` links to an `extraction_attempts` row whose `run_id` is the run.

The verifier's snapshot repository query uses these as alternative ownership proofs and returns each `asset_snapshots.id` once, so a snapshot reachable through both paths is not double-counted as two logical snapshot references. Search-response and extraction-attempt raw/normalized BLOB references remain separately counted logical references, while the blob verifier caches the integrity state by SHA-256 so duplicate immutable bytes cannot receive contradictory states in one verification pass.

Extraction-attempt BLOB evidence is read with a count/list conservation check in one UoW instead of offset-paging a relation whose historical ordering was not a total key. A count/list mismatch fails the evidence read rather than silently proving only a subset.

Chunk `content_sha256` remains derived text integrity and is never treated as a `BLOB_ROOT` identity.

## Smart fallback temporal intent

The fallback parser intentionally supports only the narrow deterministic forms already established by issue #302:

- explicit ISO date ranges such as `2026-08-17 through 2026-08-22`; and
- `past N days` resolved against one explicit evaluation clock.

Temporal-intent detection is broader than this supported grammar. Clear unsupported forms such as `last 5 days`, `during August 2026`, natural-language month/date ranges, weekdays, or other explicit relative time units fail closed with guidance to supply `--research-spec`. A supported temporal span is removed before checking the residual objective, so an additional unsupported temporal constraint cannot be silently discarded.

The detector does not treat a bare lexical use of `current` as temporal; for example, `electric current` remains a non-temporal objective. Contextual forms such as `current week` remain temporal.

A persisted plan whose start bound is later than the evaluation clock also fails closed at the provider-recency transport boundary. It is not coerced into `qdr:1d`, because a past-recency provider filter cannot represent future publication authority.

## Regression obligations

Independent/local validation of the remediated head must include:

- the unit fallback suite, including `last N days`, natural-language month/date constraints, a non-temporal `electric current` control, supported-plus-unsupported residual intent, and future-start provider transport;
- the verifier unit suite with the production extraction-attempt count/list contract;
- `tests/integration/test_issue302_verifier_run_assets.py` against repository-sanctioned disposable PostgreSQL, proving a `research_run_assets`-only snapshot verifies successfully and then fails closed for missing and corrupt bytes;
- the pre-existing issue #302 focused unit/integration/contract regressions;
- Ruff check and format-check on exact changed Python paths;
- Pyrefly on exact changed Python paths and zero-argument full-project `pyrefly check`; and
- the relevant broader PostgreSQL/Qdrant workflow-equivalent suites under the disposable-service contract.

No persistent PostgreSQL/Qdrant endpoint is an authorized validation target.
