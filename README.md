<!-- @format -->

# Firecrawl Research Skill

This Codex skill combines Firecrawl web acquisition with a persistent, auditable research corpus. PostgreSQL is the sole authority for workflow state, invocation state, provenance, claims, evidence, audits, and corpus identities. Content-addressed blobs retain immutable payloads, Qdrant supplies a rebuildable dense-retrieval projection, Valkey provides optional worker wakeups, and scratch directories remain local operational diagnostics.

`README.md` is the GitHub-facing overview. Agent instructions are canonical in `SKILL.md`; architecture and operator procedures are canonical in `references/`.

## Capabilities

- Query retained research through compact manifests, bounded passages, relationship expansion, and structured evidence packets.
- Combine PostgreSQL lexical candidates, the active Qdrant dense index, reciprocal-rank fusion, and local reranking.
- Acquire new evidence with `fsearch_smart`, `fsearch`, and `fscrape` while preserving raw responses and PostgreSQL provenance.
- Persist source, immutable snapshot, versioned derivation, chunk, run, invocation, batch, and retrieval-event identities.
- Drive wrapper operations through the PostgreSQL run-state machine with idempotent compare-and-swap transitions.
- Rebuild, activate, roll back, or prune fingerprinted Qdrant vector indexes without modifying authoritative corpus data.
- Reset PostgreSQL, Qdrant, Valkey, and blob storage to a guarded, migrated, empty state for clean installation and live testing.
- Map validated ResearchSpec semantics to versioned hard resource caps, stricter user limits, and immutable per-run budget snapshots.
- Construct and validate evidence packets with claim-to-passage bindings, corroboration, contradiction, qualification, duplicate detection, source-independence assessment, and token-budget enforcement.
- Run deterministic retrieval, evidence, workflow, and release benchmark suites.

## Authority model

The persistence stack has one authority boundary:

- **PostgreSQL:** authoritative runs, invocations, events, corpus identities, claims, evidence, audits, budgets, and job state.
- **Blob store:** immutable payload bytes referenced by PostgreSQL.
- **Qdrant:** rebuildable vector projection selected through `research_chunks_active`.
- **Valkey:** optional wakeups and transient coordination; never authoritative.
- **Ephemeral files:** ordinary secure temporary files may be used during processing, but never as workflow, replay, history, or corpus authority.

No filesystem workflow database or adapter mode exists. Wrappers read and write workflow state only through PostgreSQL services.

## First use

Resolve `<skill-root>` to the directory containing `SKILL.md`.

```bash
cd "<skill-root>"
source scripts/research-env

scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Inspect retained evidence before acquiring new material:

```bash
scripts/research-db corpus-overview
scripts/research-db search-assets "<query>" --limit 20
scripts/research-db fetch-passages "<candidate-id>" --max-tokens 2000
scripts/research-db expand-relationships "<candidate-id>" --max-hops 1
```

Start a run and attach acquisition to it:

```bash
RUN_ID="$(scripts/frun start '<research objective>')"

scripts/fscrape \
  'https://example.com/article' \
  --research-run-id "$RUN_ID"

# Or execute the coverage-led workflow. It creates a run automatically when
# --research-run-id is omitted.
scripts/fsearch_smart '<research objective>' --research-run-id "$RUN_ID"
```

Supported acquisition wrappers create PostgreSQL invocation records, retain immutable payload bytes under `BLOB_ROOT`, commit ingestion batches, and advance the run only through permitted lifecycle transitions. They report stable authoritative identifiers rather than storage paths.

## Authoritative acquisition

Supported `fsearch` and `fscrape` commands require `DATABASE_URL` to identify a writable PostgreSQL store and require a valid `fr_<uuid>` research run before any Firecrawl request starts. Successful acquisition always persists authoritative metadata and immutable payload bytes under `BLOB_ROOT`; there is no scratch-only success mode.

`fsearch_smart --dry-run` may generate a deterministic plan without database or network writes. Normal smart-search execution remains PostgreSQL-authoritative.

Explicit `DATABASE_URL`, Qdrant/Valkey endpoints and keys, blob root, and `FIRECRAWL_RESEARCH_PYTHON` take precedence over values loaded by `scripts/research-env`.

## Clean datastore initialization

The PostgreSQL-only schema baseline intentionally requires a clean database. Reset the research stack after installing this version.

Use `scripts/reset-firecrawl-research` only when existing research assets do not need to be retained. It removes:

- the PostgreSQL authoritative data volume;
- the Valkey persistence volume;
- Qdrant data and snapshots;
- the content-addressed research blob corpus.

It preserves repository files, Compose configuration, source secret files, and published Git tags/releases.

Default locations:

```text
PostgreSQL Compose: /opt/containers/research-postgres
Qdrant Compose:     /opt/containers/research-qdrant
Valkey Compose:     /opt/containers/research-valkey
Blob root:          /opt/containers/research-assets/blobs

PostgreSQL volume:  research_postgres_data
Valkey volume:      research_valkey_data
Qdrant runtime:     research_qdrant_runtime_secrets
```

Interactive reset:

```bash
cd "<skill-root>"
scripts/reset-firecrawl-research
```

Fast-forward `main` first:

```bash
scripts/reset-firecrawl-research --pull
```

Noninteractive execution after reviewing every target:

```bash
scripts/reset-firecrawl-research --pull --yes
```

A successful reset:

1. stops the persistent worker and all three datastores;
2. removes PostgreSQL and Valkey state;
3. clears Qdrant data, snapshots, and research blobs;
4. starts clean PostgreSQL, Qdrant, and Valkey services;
5. migrates PostgreSQL to `0038_postgres_authority`;
6. creates and activates an empty current-fingerprint Qdrant collection;
7. starts the persistent worker unless `--no-start-worker` is supplied;
8. enforces a healthy `research-db doctor` result;
9. verifies that authoritative corpus tables contain zero rows.

Expected clean-state properties include `schema.at_head=true`, zero corpus rows, one empty active index definition, zero Qdrant coverage discrepancies, healthy Valkey, and healthy embedding/reranking endpoints.

## PostgreSQL-backed run workflow

A low-level acquisition wrapper may advance a run through only the stages its operation actually reaches:

```text
created → planning → corpus_review → acquiring
                                      ↓
                                  extracting → indexing
```

After all run-scoped index jobs complete, `frun finish` advances the terminal path:

```text
indexing → coverage_review → synthesizing → validating → completed
```

Example smoke test:

```bash
RUN_ID="$(scripts/frun start 'Live persistence smoke test')"

scripts/fscrape \
  'https://example.com' \
  --research-run-id "$RUN_ID"

# Wait until doctor reports no pending/running/dead run-scoped index work.
scripts/research-db doctor

scripts/frun finish "$RUN_ID" --outcome satisfied
scripts/frun status "$RUN_ID"
```

`run-operation-start` and `run-operation-finish` are internal wrapper boundaries. They validate the PostgreSQL run, record the invocation, and perform idempotent lifecycle transitions. Operators normally use `frun`, `fsearch`, `fscrape`, or `fsearch_smart` rather than invoking those commands directly.

## Corpus and index lifecycle

```bash
scripts/research-db migrate
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor

# Persistent worker
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5

# Fingerprinted Qdrant lifecycle
scripts/research-db index-list
scripts/research-db index-build --current-config --all
scripts/research-db reconcile-qdrant
scripts/research-db index-activate '<index-id>'
scripts/research-db index-rollback '<prior-index-id>'
scripts/research-db index-prune --dry-run

# Rebuild parser/chunker derivations or export retained records
scripts/research-db rederive --snapshot '<snapshot-id>'
scripts/research-db export-run '<run-id>' --output run.json
```

Physical Qdrant collections use `research_chunks_<12-character-fingerprint>`. Retrieval uses `research_chunks_active`. Dense retrieval is enabled only when the alias and schema match the configured embedding fingerprint; otherwise retrieval remains lexical and `doctor` reports the mismatch.

## Run provenance and lifecycle commands

```bash
RUN_ID="$(scripts/frun start '<research objective>' --mode autonomous_local)"

scripts/fsearch '<query>' --research-run-id "$RUN_ID"
scripts/fscrape '<url>' --research-run-id "$RUN_ID"
scripts/research-db search-assets '<query>' --research-run-id "$RUN_ID" --limit 20
scripts/research-db fetch-passages '<candidate-id>' --research-run-id "$RUN_ID" --max-tokens 2000

scripts/frun annotate "$RUN_ID" \
  --type pivot \
  --reason 'switched focus to primary specification'

scripts/frun finish "$RUN_ID" \
  --outcome satisfied \
  --source-manifest sources.json \
  --answer-file final.md \
  --auto-audit

scripts/frun status "$RUN_ID"
scripts/frun reopen "$RUN_ID" --reason 'acquire additional corroboration'
scripts/frun cancel "$RUN_ID" --reason 'operator request'
```

PostgreSQL transitions use a strict state matrix, monotonic lifecycle revisions, and idempotency keys. Retry an uncertain command with the same key. After a stale-revision rejection, inspect `run-status` before issuing a new command.

## Evidence packet management

```bash
scripts/research-db build-evidence-packet '<id-1>' '<id-2>' --max-tokens 3000
scripts/research-db packet-validate "$RUN_ID" --include-warnings
scripts/research-db packet-inspect "$RUN_ID" --bounded --max-passages 20 --max-claims 10
scripts/research-db packet-diff "$RUN_ID" --old-revision N --new-revision N
scripts/research-db packet-export "$RUN_ID" --output packet.json --bounded
```

Evidence packets contain claims, passages, bindings, relationship groups, duplicate groups, independence assessments, and retrieval provenance. Validation checks referential integrity, claim coverage, group completeness, freshness, unresolved requirements, token budget, retrieval provenance, and semantic-stage completeness.

## Validation

Run the deterministic and integration tests:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider scripts/
```

CI runs Python 3.11 and 3.12 against disposable PostgreSQL and Qdrant services, plus Ruff lint/format checks and strict campaign contract tests.

For design invariants, read `references/research-store-architecture.md`. For deployment, clean reset, backup/restore, worker, indexing, and recovery procedures, read `references/operations-runbook.md`. For schema and command changes, read `references/migration-guide.md` and `references/workflow-state-schema.md`.
