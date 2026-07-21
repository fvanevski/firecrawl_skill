# Legacy workflow baseline for P1-01

This report freezes the observable behavior required by GitHub issue #2 before
later orchestration or authoritative-state refactors. It is a regression
baseline, not a claim that every recorded behavior is desirable.

## Replay surface

`tests/fixture_replay.py` implements the Firecrawl CLI boundary used by the
legacy wrappers. It accepts recorded search and scrape responses selected by
`FIRECRAWL_REPLAY_FIXTURE`; it has no network or model client. Scenario and
golden files live below `tests/fixtures/legacy_baseline/`, and `manifest.json`
pins every fixture byte-for-byte with SHA-256.

`golden/state_records.json` captures normalized PostgreSQL row shapes and event
records with fixed identifiers. Its regression test checks source-to-snapshot,
snapshot-to-document, document-to-chunk, chunk-to-manifest, manifest-to-job,
batch-to-asset, retrieval-candidate, index-definition, and lease-token
references. Volatile timestamps and generated UUIDs are normalized only in the
fixture; the live disposable-database tests continue to validate real rows.

The replay suite covers:

| Scenario | Direct search | Scrape payload | Recorded semantic output | Expected result |
| --- | --- | --- | --- | --- |
| Technical troubleshooting | yes | yes | brief and query plan | two candidates |
| Legislative/legal | yes | yes | brief | two official candidates |
| Breaking news | yes | yes | brief | two current reports |
| Academic debate | yes | yes | brief | two contrasting scholarly sources |
| No result | yes | none | brief | successful empty manifest |

The technical scenario also replays the smart-search wrapper with its existing
deterministic heuristic planner and a zero scrape budget. The legal scenario
replays the direct scrape wrapper. Recorded semantic outputs are injected at
the structured-output seam, proving that regression tests do not call a
generative endpoint.

## Intentional invariants

These are compatibility and correctness constraints, not candidates for
normalization during later phases:

- PostgreSQL is authoritative for retained corpus state and ingestion batches.
  A configured persistence outage returns nonzero and writes a failed
  `_corpus.json` repair manifest; scratch success is not authoritative success.
- Raw and normalized payloads remain separate. Content-addressed blob identity
  is SHA-256 based, immutable, deduplicated, and independently verifiable.
- Identical source bytes reuse the same snapshot; changed bytes create a new
  snapshot with lineage. Parser, normalizer, and chunker upgrades create new
  derivations rather than false source snapshots.
- Index jobs are bound to the exact embedding manifest, definition fingerprint,
  physical collection, model, dimension, and distance metric.
- Only the current lease token may renew or finish a job. A worker that loses
  its lease after an idempotent Qdrant upsert does not claim completion.
- Qdrant remains a rebuildable projection. A worker-side Qdrant failure is
  recorded on the durable PostgreSQL job and does not become completion.
- Valkey notification is best effort. Lost or unavailable notifications do not
  replace PostgreSQL polling and therefore cannot strand durable work.
- Candidate, chunk, snapshot, source, research-run, retrieval, model, and audit
  provenance identifiers remain explicit. Model-inferred relations require
  model provenance; invented evidence references are rejected.
- Scratch manifests, `_index.md`, `_context.json`, `_candidates.json`, and
  Catalog v5 records remain compatibility/debug/audit surfaces. Their creation
  alone never establishes authoritative persistence.

The database-backed assertions remain in
`scripts/test_research_store_integration.py`: snapshot versioning and
transactional jobs, bounded passage provenance, concurrent idempotent ingest,
configured derivation selection, partial-batch ledgers, run immutability,
final-attempt expiry, exact lease-token completion, and manifest-definition
binding. They require an explicitly disposable PostgreSQL database and retain
their destructive-test guard.

## Recorded failure behavior

| Failure | Baseline behavior | Classification |
| --- | --- | --- |
| PostgreSQL persistence outage | wrapper returns nonzero and exports failed asset records | intentional fail-closed guarantee |
| Valkey outage | notification returns false; durable polling remains available | intentional degraded behavior |
| Generative model outage | research brief falls back to a conservative brief and records degraded provenance | legacy compatibility behavior |
| Qdrant index-worker outage | durable job is finished as failed with the error; no completion is reported | intentional recovery guarantee |
| Qdrant retrieval outage | lexical results are returned but the degradation is silently omitted | known legacy defect; strict `xfail` for PRD FR-013 |
| No search candidates | wrapper succeeds with zero candidates and zero scrapes | intentional empty-result behavior |

The Qdrant retrieval defect is deliberately not fixed in P1-01. Later retrieval
work must turn the strict expected failure into a passing explicit-degradation
assertion rather than treating silent fallback as correct.

## Golden normalization

Golden comparisons exclude timestamps, generated invocation IDs, absolute
scratch paths, file sizes, and other host-dependent values. They retain the
operation, exact query, ordered candidate URLs and titles, candidate count, and
scrape count. Separate assertions verify creation and content of raw search,
candidate, context, evidence, scrape, and index manifests.

## Running the baseline

```bash
rtk pytest -q -p no:cacheprovider \
  scripts/test_classifier.py \
  scripts/test_workflow.py \
  scripts/test_research_store.py \
  scripts/test_index_runtime.py \
  scripts/test_golden_fixtures.py
```

No Firecrawl, Qdrant, Valkey, PostgreSQL, or model endpoint is contacted by this
command. Run `scripts/test_research_store_integration.py` separately only with
the documented disposable-database guard.

## Rollback and repair

This issue adds tests, fixtures, a replay helper, and this report only. Rollback
is removal or revert of those files; there is no schema or data rollback. If a
fixture changes intentionally, review the semantic and compatibility impact,
update its paired golden file, recompute the SHA-256 entry, and explain why the
baseline moved. Never refresh goldens merely to make an unexplained regression
pass.
