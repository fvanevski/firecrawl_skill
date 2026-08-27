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

Normal retained-first research uses the deterministic controller. The outer agent supplies the objective and genuine human decisions; application code owns planning, retained review, bounded acquisition, evidence preparation, lifecycle progression, and terminal delivery.

```bash
scripts/fresearch run 'Research objective'
scripts/fresearch continue 'fr_<uuid>'
scripts/fresearch status 'fr_<uuid>'
scripts/fresearch result 'fr_<uuid>'
```

Follow the returned typed disposition. Do not reconstruct internal lifecycle revisions, ResearchSpec/SearchPlan IDs, candidate-budget check parameters, or low-level `frun`/provider command sequences from logs. Normal delivery defaults to `host_handoff`, which validates and persists the authoritative evidence/coverage boundary without generating redundant inner full-prose output; `fresearch result` returns the bounded citation-ready handoff used by the host answer.

`scripts/fsearch_smart` is retained only as a deprecated exact compatibility name for `scripts/fresearch run`. It owns no independent planning, resume, dry-run, spec-skeleton, or recovery policy.

### Specialist direct acquisition

`fsearch`, `fscrape`, `frun`, `finspect`, `research-db`, and `candidate-budget` remain explicit specialist/operator/debug surfaces. Direct provider acquisition fails closed and requires a writable authoritative store plus an acquisition-eligible prepared run before any Firecrawl process or network transport is invoked.

```bash
RUN_ID="$(
  scripts/frun start 'Specialist acquisition' \
    --run-mode curated \
    --mode autonomous_local
)"
scripts/frun prepare "$RUN_ID"

scripts/fsearch 'bounded query' \
  --research-run-id "$RUN_ID" \
  --limit 20 \
  --scrape-limit 5

scripts/fscrape 'https://example.com/article' \
  --research-run-id "$RUN_ID"

python3 scripts/drain_index_jobs.py \
  --research-run-id "$RUN_ID" \
  --batch-size 64
scripts/research-db run-status "$RUN_ID"
scripts/frun cancel "$RUN_ID" --reason 'specialist acquisition example complete'
```

This specialist example deliberately cancels instead of fabricating curation, coverage, or completion authority. For normal research, use `fresearch` rather than translating its typed directives into low-level lifecycle commands.

`research-db worker --once` handles at most one bounded batch. Run-scoped `drain_index_jobs.py` consumes the authoritative run/job census with bounded recovery behavior; its success or exit status is projection evidence, not research-objective completion authority. Unscoped use remains available for projection maintenance.

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

For the canonical controller, human-action, final-delivery, and projection-recovery sequences, read `SKILL.md` and `references/authoritative-workflows.md`. Additional deployment and compatibility references:

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
  pytest -q -p no:cacheprovider tests/
```

Disposable PostgreSQL, Qdrant, Valkey, worker, and live-validation suites are required for changes touching those contracts. CI records the exact tested commit SHA.
