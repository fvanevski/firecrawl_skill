# Issue #300 — PR #301 review remediation ledger

This note records the narrow revisions made after the independent review of PR
#301 at commit `0ba22a17a4da41cc390e86877bd42b05ac6a4a0c`. It is supplemental to
`curated-run-lifecycle.md`; the lifecycle document remains the operator-facing
contract for curated research. This ledger exists so review findings, their
canonical fixes, and the required regression evidence remain traceable.

The original review disposition was `CHANGES_REQUESTED`. Nothing in this note
changes that disposition. A separate reviewer must review the final exact PR
head after all required static/runtime authorities and local disposable-service
validation succeed. The PR must not be merged merely because this remediation
ledger exists or because a subset of CI is green.

## 1. Exact-head Pyrefly authority

### Review finding

The changed-Python-scope Pyrefly job failed with 21 errors. The failures were in
new/modified issue-300 tests and test doubles, including unchecked
`cursor.fetchone()` results, dynamically formatted psycopg SQL, protocol-
incompatible scrape adapters, structurally invalid budget/coverage stand-ins,
inconsistent cursor override return types, monkeypatched method signatures, and
non-Mapping arguments to the temporal ranking seam.

### Resolution

The tests now conform to the repository's production type contracts instead of
suppressing diagnostics:

- PostgreSQL scalar reads assert a row exists before subscripting.
- Dynamic table identifiers use `psycopg.sql.Identifier`/`sql.SQL` composition.
- Direct-scrape test adapters implement the complete `DirectScrapeAdapter`
  signature.
- Budget tests use `CandidateBudget`, not untyped structural stand-ins.
- Cursor test doubles share compatible return annotations.
- fscrape method seams are exercised through a typed test subclass instead of
  assigning incompatible lambdas to bound methods.
- The curated coverage test double is explicitly cast to the production
  `CompleteCoverageService` seam.
- Temporal ranking tests pass `Mapping` candidates as required by the service
  contract.
- The temporal terminal integration fixture uses the declared invariant list
  type accepted by `CorpusService.ingest_batch`.

No Pyrefly configuration, baseline, scope, or suppression was changed.

### Required evidence

Before a positive review disposition, both of these must succeed on the same
exact PR head:

1. `pyrefly check <all changed ACMR Python paths>` including changed tests;
2. repository-root `pyrefly check` with no file arguments.

A passing source-only Pyrefly invocation is not a substitute for either check.

## 2. Authoritative PostgreSQL storage-gate isolation

### Review finding

The new extraction-budget regression persisted common canonical source URLs in
a disposable PostgreSQL service. A later database-native inspection fixture
assumed the same canonical source URL was globally unused, producing a
`sources_canonical_url_key` unique violation in both authoritative-storage
jobs. The failure was order-dependent shared-test state, not a database schema
fault.

### Resolution

The database-native inspection fixture now derives canonical URLs from its
unique run UUID. Canonical source identity therefore remains globally unique
inside a shared disposable-service lifecycle regardless of tests that ran
before it. Database uniqueness constraints and storage authority are unchanged.

The authoritative storage tests must continue to run against the repository's
disposable PostgreSQL helper; persistent personal services are not valid test
targets.

### Required evidence

Run, in one disposable PostgreSQL lifecycle, the extraction-budget regression
followed by `test_database_native_acceptance_paths`, then run the complete
`Authoritative storage gates` family for Python 3.11 and 3.12. No fixture may
require global absence of another test's fixed canonical source identity.

## 3. Future timestamps are not temporal authority

### Review finding

`freshness_satisfied()` accepted any timestamp newer than the lower cutoff and
had no upper bound at the evaluation clock. Exact-recency ranking also treated a
negative age (future publication) as satisfied. Future-dated provider metadata
could therefore satisfy ranking, evidence freshness, coverage, and terminal
admission.

### Resolution

Temporal qualification is now an observed-time interval, not a one-sided lower
bound:

```text
reference_time - max_age <= authoritative_timestamp <= reference_time
```

For an explicit publication window, a publication timestamp must both lie in
the declared window and be no later than the evaluation clock. A declared
window ending in the future does not authorize future provider metadata.

Exact-recency ranking marks future publication as `UNSATISFIED` and applies the
normal stale/unsatisfied ranking penalty. Retrieval time is still not promoted
to publication or update authority.

### Regression coverage

The issue-300 temporal tests now cover:

- future publication rejected by exact recency ranking;
- future publication rejected by an explicit publication window;
- future publication rejected by numeric freshness;
- future explicit update rejected by numeric freshness;
- transactional terminal rejection for future publication;
- transactional terminal rejection for an old publication with a future update;
- transactional publication-window rejection for future publication.

Existing lower-bound, missing-date, retrieval-only, out-of-window, and
old-publication/recent-valid-update cases remain in place.

## 4. Single PostgreSQL temporal authority

### Review finding

`temporal_postgres.py` duplicated temporal candidate/repository/UoW behavior
already installed in the canonical `postgres_uow_core.py` composition path. The
extra module was not used by production composition and created an unnecessary
second apparent authority surface.

### Resolution

The duplicate implementation has been retired. `temporal_postgres.py` is now a
non-authoritative import tombstone and exposes no repository or UoW symbols.
Temporal candidate normalization and run-locked EvidencePacket persistence are
owned solely by the canonical repository context in `postgres_uow_core.py`.

The file may be deleted mechanically in a later cleanup only if repository
reference checks confirm there are no external/import compatibility consumers;
its presence in the current branch does not provide a second implementation or
runtime authority.

## 5. Automated Codex review thread — stale synthesis-stage reuse

The exact inline prose of the automated Codex comment was not available through
the focused review-read surface used for this remediation. The sole unresolved
thread was bound to `curated_synthesis_service.py` at the stale-stage reset
predicate. This remediation therefore addresses the code-level invariant at
that location without claiming an uninspected quotation.

### Resolution

A completed synthesis stage is reusable only when its persisted authority
fingerprint matches all current inputs that determine stage semantics:

- EvidencePacket revision;
- persisted model identity (including the empty host-agent identity contract);
- prompt version;
- schema version.

`CuratedSynthesisService._reset_stale_stages()` now uses PostgreSQL
`IS DISTINCT FROM` comparisons for all four fields. Any mismatch resets the
stage to `pending`, clears semantic-call/artifact pointers and the stage
artifact/error, and writes the current fingerprint. Historical semantic calls
and immutable semantic artifacts are retained as provenance; only current-stage
pointers are invalidated.

A PostgreSQL integration regression mutates model identity while keeping the
same packet revision and proves that all stale stages reset. The pre-existing
packet-revision invalidation regression remains.

## 6. Test and documentation closure gaps

The following evidence is required before the remediation is considered closed:

1. **Ruff changed scope:** `ruff check` and `ruff format --check --diff` on exact
   ACMR Python paths.
2. **Pyrefly changed scope:** explicit changed Python paths, including tests.
3. **Focused unit/runtime regressions:** temporal policy, fscrape identity,
   replay-safe budget, curated synthesis, temporal terminal guard, and the
   database-native storage collision sequence.
4. **Full-project Pyrefly:** repository-root `pyrefly check` with committed
   configuration and pinned version.
5. **Broader runtime authority:** applicable acquisition/fscrape/fsearch,
   authoritative-storage, curated completion-provenance, contract, integration,
   and release-gate suites.
6. **Disposable-service boundary:** every test that may reset PostgreSQL or
   mutate Qdrant must use `scripts/disposable-test-services`; never use personal
   PostgreSQL port 55432 or Qdrant port 6333.
7. **Cleanup evidence:** helper `down` succeeds and no helper-owned containers
   remain.
8. **Exact-head review:** after all checks, re-resolve the PR head and have a
   separate reviewer independently inspect that exact revision. Prior reviews
   of `0ba22a17...` are stale after these remediation commits.

## Local-agent handoff boundary

The local agent is an evidence collector for the final validation pass. It may
use Serena for changed-symbol/reference inspection, RTK for routine successful
Ruff/Pyrefly/pytest output, and native Git/PostgreSQL/Docker commands for exact
SHA, failures, database/runtime and cleanup evidence. Probe is unnecessary
unless the relevant repository location is genuinely unknown. OpenViking may
supply bounded historical rationale only; it is not authority for current
source, Git, CI, database, or runtime state.

The local agent must not redesign production behavior, weaken a gate, modify
Pyrefly configuration/baseline, or repair production/test code merely to make a
check green. Any failure is returned to Central as evidence for further
remediation.
