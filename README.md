# Firecrawl Research Skill

This Codex skill combines Firecrawl web acquisition with a persistent, auditable research corpus. PostgreSQL is authoritative, content-addressed blobs retain immutable payloads, Qdrant supplies a rebuildable dense-retrieval projection, and Valkey provides optional worker wakeups. Scratch directories and Catalog v5 remain available for compatibility, debugging, and acquisition audits.

`README.md` is intentionally retained as the GitHub-facing repository overview. Agent instructions remain canonical in `SKILL.md`; architecture and operator procedures remain canonical in `references/`.

## Capabilities

- Query retained research through compact manifests, bounded passages, relationship expansion, and structured evidence packets.
- Combine PostgreSQL lexical candidates, the active Qdrant dense index, reciprocal-rank fusion, and local reranking.
- Acquire new evidence with `fsearch_smart`, `fsearch`, and `fscrape` while preserving raw responses and provenance.
- Persist source, immutable snapshot, versioned derivation, chunk, run, batch, and retrieval-event identities.
- Rebuild, activate, roll back, or prune fingerprinted Qdrant vector indexes without modifying authoritative corpus data.
- Manage multi-step research runs (`fr_<uuid>`) with explicit lifecycle states, audits, pivots, and Catalog v5 provenance.

## First use

Resolve `<skill-root>` to the directory containing `SKILL.md`, and keep `rtk proxy` at the agent-visible boundary.

```bash
# Inspect and retrieve retained assets first.
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
rtk proxy "<skill-root>/scripts/research-db" expand-relationships "<candidate-id>" --max-hops 1
rtk proxy "<skill-root>/scripts/research-db" build-evidence-packet "<candidate-id-1>" "<candidate-id-2>"

# Acquire new evidence only when the retained corpus is insufficient.
rtk proxy "<skill-root>/scripts/fsearch_smart" "<research objective>"
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article"
```

Wrappers write `firecrawl_scratch/fc_<uuid>/...` artifacts. When persistence is enabled they also commit an invocation batch and produce `_corpus.json` with stable source, snapshot, document, and chunk IDs.

## Persistence modes

Set `FIRECRAWL_RESEARCH_PERSIST=auto|on|off`:

- `auto` persists when a database resolves and otherwise keeps the filesystem workflow.
- `on` requires a healthy persistence configuration (`ingest-ready`) before acquisition.
- `off` disables database and raw-blob persistence.

Enabled persistence is fail-closed. If any successful Firecrawl result cannot be retained, the wrapper preserves diagnostics and the partial corpus manifest but returns nonzero; it does not silently downgrade to scratch-only success.

Use both switches for private acquisition:

```bash
FIRECRAWL_CATALOG_DISABLED=1 FIRECRAWL_RESEARCH_PERSIST=off \
  rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/private"
```

Explicit `DATABASE_URL`, Qdrant/Valkey endpoints and keys, blob root, and `FIRECRAWL_RESEARCH_PYTHON` take precedence over `scripts/research-env`. See `references/research-store-operations.md` for the full configuration surface.

## Corpus and index lifecycle

```bash
rtk proxy "<skill-root>/scripts/research-db" migrate
rtk proxy "<skill-root>/scripts/research-db" status
rtk proxy "<skill-root>/scripts/research-db" ingest-ready
rtk proxy "<skill-root>/scripts/research-db" doctor

# Run persistently through firecrawl-research-indexer.service.
rtk proxy "<skill-root>/scripts/research-db" worker --batch-size 32 --poll-seconds 5 --lease-seconds 300 --max-attempts 5

# Build a fingerprinted physical collection, verify it, and switch the stable alias.
rtk proxy "<skill-root>/scripts/research-db" index-list
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all
rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-prune --dry-run

# Rebuild parser/chunker derivations or reconstruct an interrupted compatibility export.
rtk proxy "<skill-root>/scripts/research-db" rederive --snapshot "<snapshot-id>"
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output _corpus.json
rtk proxy "<skill-root>/scripts/research-db" import-scratch "<scratch-dir>" --dry-run
```

Physical Qdrant collections use `research_chunks_<12-character-fingerprint>`; retrieval uses the stable `research_chunks_active` alias. Dense retrieval runs only when that alias matches the configured fingerprint, otherwise queries remain lexical and `doctor` reports the mismatch. PostgreSQL jobs carry leases and exact embedding-manifest identities, so crashed workers can be reclaimed without losing or misattributing work. Default `doctor` is read-only.

## Research-run provenance

```bash
RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start "<research objective>" --profile auto)"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --research-run-id "$RUN_ID" --limit 20
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --research-run-id "$RUN_ID" --max-tokens 2000
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied --source-manifest sources.json --answer-file final.md

# Manage run lifecycle, pivots, and audits
rtk proxy "<skill-root>/scripts/frun" reopen "$RUN_ID" --reason "acquire official whitepaper"
rtk proxy "<skill-root>/scripts/frun" annotate "$RUN_ID" --type pivot --reason "switched focus to primary spec"
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --llm local
rtk proxy "<skill-root>/scripts/frun" compare "$RUN_ID" "$OTHER_RUN_ID"
rtk proxy "<skill-root>/scripts/frun" purge --keep-last 10
```

The shared `fr_<uuid>` links catalog chronology, acquisition batches, retained assets, retrieval events, selected evidence, the source manifest, and the delivered-answer hash. Explicit research runs enforce lifecycle terminal invariants (`running`, `finished`, `cancelled`).

## Validation

Run the full deterministic suite without network access:

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  "<skill-root>/scripts/test_classifier.py" \
  "<skill-root>/scripts/test_workflow.py" \
  "<skill-root>/scripts/test_research_store.py" \
  "<skill-root>/scripts/test_index_runtime.py"
```

Run `scripts/test_research_store_integration.py` only against an explicitly named disposable PostgreSQL target whose name contains a standalone `test` segment, and set `RESEARCH_STORE_TEST_ALLOW_RESET` to that exact name. Its guarded session setup drops the public schema and covers the non-empty migration (v1 through v5), database concurrency, derivations, retry ledgers, leases, runs, and manifest binding. Use a separate recorded disposable-service campaign for wrapper preflight/fail-closed behavior, Valkey loss, damaged Qdrant rebuild, alias activation, and rollback proofs required before production.

For the design invariants, read `references/research-store-architecture.md`. For deployment, migration, backup/restore, worker, indexing, and recovery procedures, read `references/research-store-operations.md`. For Catalog v5 manifests and semantic audits, read `references/catalog-v5.md`.
