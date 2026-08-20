# Retrieval and projection capability boundary

This document describes the **final** topology after issue #269. The migration-era dual-owner/facade arrangement from issue #263 is no longer supported.

## Authority invariants

- PostgreSQL owns durable retrieval provenance, canonical corpus text, index definitions, manifests, jobs, leases, checkpoint state/observations, and lifecycle transitions.
- Qdrant is a rebuildable dense-vector projection. It owns no canonical research data and cannot authorize lifecycle completion.
- Retrieval hard bounds, index fingerprints, alias activation, lease/retry rules, checkpoint replay/finalization, census behavior, and reconciliation safeguards remain semantic invariants across the structural cleanup.
- Pyrefly remains the sole static type-check authority. The cleanup may remove stale baseline records, but must not broaden the baseline, add broad suppressions, weaken scope/configuration, or change checker version merely to obtain green status.

## Final ownership map

| Responsibility | Canonical owner |
|---|---|
| Ranking/RRF/context packing/reranking | `firecrawl_skill.research_store.retrieval.ranking` |
| Retrieval orchestration | `firecrawl_skill.research_store.retrieval.service` |
| Retrieval provenance persistence | `firecrawl_skill.research_store.retrieval.postgres` |
| Qdrant projection client | `firecrawl_skill.research_store.retrieval.projection.qdrant` |
| Cross-store alias authority | `firecrawl_skill.research_store.retrieval.projection.authority` |
| Projection reconciliation | `firecrawl_skill.research_store.retrieval.projection.reconciliation` |
| Durable index-job repository | `firecrawl_skill.research_store.retrieval.projection.postgres_jobs` |
| Index worker/embedder | `firecrawl_skill.research_store.retrieval.projection.indexing` |
| Checkpoint orchestration | `firecrawl_skill.research_store.retrieval.projection.checkpoint_indexing_stage` |
| Checkpoint models/store/replay/finalization/service | `firecrawl_skill.research_store.retrieval.projection.index_checkpoint_*` |

## Import contract

`firecrawl_skill.*` is the only production package identity. Installed code and source tests must not depend on `scripts/` to obtain production modules and must not synthesize a second `research_store.*` package identity.

The following migration-only siblings/facades are forbidden in the final tree:

- `research_store/retrieval.py` and `retrieval_core.py`;
- root `retrieval_service.py` and `postgres_retrieval.py`;
- root `qdrant.py`, `qdrant_authority.py`, and `projection_reconciliation.py`;
- root `indexing.py`, `checkpoint_indexing_stage.py`, and `index_checkpoint_*.py`.

Supported callers import the canonical owners directly. New compatibility aliases require a separately documented external contract and may not be introduced as an implementation shortcut.

## Tests and exact-head evidence

`tests/unit/test_issue_263_retrieval_projection_slice.py` is now a final-state regression. It proves canonical implementation `__module__` ownership and physical absence of the migration facades rather than preserving identity with them.

`tests/contract/test_package_boundary.py` builds an isolated wheel and proves that the installed distribution contains canonical `firecrawl_skill.*` modules but no top-level script modules or legacy package roots.

`.github/workflows/retrieval-projection-slice-review.yml` must track the final `retrieval/projection/**` owner paths. Exact-head validation remains cumulative: changed-scope Ruff, changed-scope and full-project Pyrefly, focused retrieval/checkpoint/reconciliation/protection tests, isolated-wheel checks, and disposable PostgreSQL/Qdrant integration evidence where mutation is required.
