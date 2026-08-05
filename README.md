<!-- @format -->

# Firecrawl Research Skill

Firecrawl Research Skill acquires web evidence through Firecrawl and retains it in an auditable research corpus.

## Target A authority model

The current release implements **Target A**:

- **PostgreSQL** is authoritative for research runs, invocations, workflow transitions, acquisition provenance, corpus identities, retrieval events, evidence, audits, and durable jobs.
- **`BLOB_ROOT`** is the immutable content-addressed store for provider payload bytes referenced by PostgreSQL. Payload bytes are not migrated into PostgreSQL in Target A.
- **Qdrant** is a rebuildable dense-retrieval projection selected through the `research_chunks_active` alias.
- **Valkey** is optional transient coordination. Workers recover durable work by polling PostgreSQL.
- **Ephemeral files** may support bounded in-process operations, but are never workflow, replay, history, selection, or corpus authority.

A future design that stores payload bytes in PostgreSQL would be a separate migration target. It is not implemented or implied by this release.

## First use

Resolve `<skill-root>` to the directory containing `SKILL.md`.

```bash
cd "<skill-root>"
source scripts/research-env

scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Supported acquisition fails closed. `fsearch` and `fscrape` require a writable authoritative store and a valid nonterminal `fr_<uuid>` before any Firecrawl process or network transport is invoked.

```bash
RUN_ID="$(scripts/frun start 'Research objective')"

scripts/fsearch 'bounded query' \
  --research-run-id "$RUN_ID" \
  --limit 20 \
  --scrape-limit 5

python3 scripts/drain_index_jobs.py \
  --research-run-id "$RUN_ID" \
  --batch-size 64
scripts/research-db run-status "$RUN_ID"
scripts/frun finish "$RUN_ID" --outcome satisfied
scripts/frun status "$RUN_ID"
```

`research-db worker --once` handles at most one bounded batch. Run-scoped `drain_index_jobs.py` seals the run's current PostgreSQL chunk membership, consumes the exact index-job census, waits with bounded backoff for live leases, reclaims expired work, retries recoverable failures, and succeeds only when every expected manifest is complete. A run-scoped drain has a 300-second default deadline; a recoverable deadline or batch bound returns structured `index-drain-result-v1` output with exit status `75`. SIGINT or SIGTERM is observed during scoped setup, before and after every worker batch, and during backoff; cancellation takes precedence over an otherwise complete observation and returns `130`. Neither outcome advances the run lifecycle. Unscoped use remains available for projection maintenance, retains its historical max-batch-only default with no elapsed deadline unless `--deadline-seconds` is explicitly supplied, and its `claimed=0` result is not run-completion evidence.

To add a direct scrape to an existing nonterminal run, drain and verify prior work first, then drain again after the scrape:

```bash
python3 scripts/drain_index_jobs.py --research-run-id "$RUN_ID" --batch-size 64
scripts/fscrape 'https://example.com/article' --research-run-id "$RUN_ID"
python3 scripts/drain_index_jobs.py --research-run-id "$RUN_ID" --batch-size 64
scripts/research-db run-status "$RUN_ID"
```

`fsearch_smart` creates a run when `--research-run-id` is omitted. Its `--dry-run` mode emits a deterministic plan without database or network writes.

```bash
scripts/fsearch_smart 'Research objective'
scripts/fsearch_smart 'Research objective' --research-run-id "$RUN_ID"
scripts/fsearch_smart 'Research objective' --dry-run
```

## Stable inspection, replay, and selection

Use stable PostgreSQL identifiers rather than local paths or result ranks.

```bash
scripts/finspect runs --limit 20
scripts/finspect invocations --run "$RUN_ID" --limit 20
scripts/finspect search-responses --run "$RUN_ID" --limit 20
scripts/finspect replay-search '<search-response-uuid>'
scripts/finspect scrape-candidates '<candidate-uuid>' \
  --idempotency-key '<stable-key>'
scripts/finspect inspect '<asset-uuid>'
scripts/finspect passages '<asset-uuid>' --max-tokens 2000
```

`replay-search` reads retained provider bytes from `BLOB_ROOT` after integrity verification and does not invoke Firecrawl. Candidate acquisition accepts stable candidate UUIDs and performs authoritative preflight before provider execution.

## Retrieval and projection lifecycle

```bash
scripts/research-db corpus-overview
scripts/research-db search-assets 'query' --limit 20
scripts/research-db inspect-asset '<candidate-id>'
scripts/research-db fetch-passages '<candidate-id>' --max-tokens 2000

scripts/research-db index-list
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
scripts/research-db index-activate '<index-id>'
scripts/research-db index-rollback '<prior-index-id>'
```

Qdrant loss does not destroy authoritative data. Rebuild a compatible fingerprinted collection from PostgreSQL chunks and activate it only after complete job processing and reconciliation. Valkey loss requires no data repair; restart it and the worker continues from PostgreSQL jobs.

## Explicit exports

Exports are user-requested presentation artifacts, not runtime state.

```bash
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
```

For the canonical acquisition, completion, and projection-recovery sequences, read `references/authoritative-workflows.md`. Additional deployment and compatibility references:

- `references/operations-runbook.md`
- `references/research-store-operations.md`
- `references/migration-guide.md`
- `references/recovery-drill-checklist.md`
- `references/release-notes-rc9.md`

The RC-9 release notes define the breaking compatibility boundary and the exact pre-removal revision used to import legacy acquisition trees.

## Validation

```bash
ruff check .
ruff format --check .
env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider scripts/
```

Disposable PostgreSQL, Qdrant, Valkey, worker, and live-validation suites are required for changes touching those contracts. CI records the exact tested commit SHA.
