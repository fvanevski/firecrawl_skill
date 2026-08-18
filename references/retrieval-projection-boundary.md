# Retrieval and projection capability boundary

Issue #263 consolidates retrieval application behavior and derived index/projection
infrastructure without changing persistence authority or retrieval semantics.
The refactor is structural: PostgreSQL remains durable authority and Qdrant
remains a rebuildable dense projection.

## Authority invariants

- PostgreSQL owns durable retrieval provenance, index definitions, manifests,
  index jobs, leases, checkpoint state/observations, lifecycle transitions, and
  the canonical corpus text used by lexical retrieval.
- Qdrant owns no canonical research data. It is a rebuildable dense projection
  derived from PostgreSQL-authoritative corpus/index state.
- Index fingerprints, alias activation rules, lease/retry behavior, checkpoint
  replay/finalization, census behavior, and reconciliation protection semantics
  are unchanged by #263.
- Retrieval passage/evidence/relationship hard bounds remain unchanged.
- `pyrefly-baseline.json` is pre-existing debt authority only; #263 does not
  expand it or relocate debt-bearing checkpoint/indexing implementations in a
  way that turns historical path-keyed debt into new diagnostics.

## Boundary map

| Responsibility | Canonical location | Contract |
|---|---|---|
| Ranking/RRF/context packing/reranking | `research_store.retrieval.ranking` | Retrieval application support; preserves historical reranker patch hooks. |
| Retrieval orchestration | `research_store.retrieval.service` | PostgreSQL lexical retrieval plus optional derived Qdrant dense retrieval; preserves degradation/provenance and hard bounds. |
| Retrieval provenance persistence | `research_store.retrieval.postgres` | Connection-bound PostgreSQL repository; no transaction ownership. |
| Qdrant projection client | `research_store.retrieval.projection.qdrant` | Rebuildable dense-vector projection only. |
| Cross-store alias authority | `research_store.retrieval.projection.authority` | Compares PostgreSQL-active definition with required Qdrant alias; PostgreSQL lifecycle remains authoritative. |
| Projection reconciliation | `research_store.retrieval.projection.reconciliation` | Observes/repairs current derived projection under existing safeguards. |
| Durable index-job repository | `research_store.retrieval.projection.postgres_jobs` | PostgreSQL-authoritative job/lease/completion persistence. |
| Index worker/embedder | `research_store.retrieval.projection.indexing` | Zero-domain-logic facade over the baseline-stable historical implementation. |
| Checkpoint family | `research_store.retrieval.projection.index_checkpoint_*` and `checkpoint_indexing_stage` | Zero-domain-logic facades over baseline-stable historical implementations. |

## Supported import contexts

There are two supported source identities during the Phase 1 package-boundary
migration:

1. Canonical source/wheel imports use `firecrawl_skill.research_store.*`.
2. The historical pytest corpus inserts `scripts/` first in `scripts/conftest.py`,
   so direct source tests may load the retained implementation as
   `research_store.*`.

These contexts intentionally execute the same repository-owned implementation.
Structural tests must therefore assert the exact capability-relative module
(e.g. `research_store.retrieval.service`) without assuming that the optional
`firecrawl_skill.` prefix is present. Installed-package tests remain strict:
canonical wheel imports must expose canonical `firecrawl_skill.research_store.*`
module identity and legacy imports must resolve to those same module objects.

Changing global pytest `sys.path` ordering is not part of #263. Doing so would
alter the import assumptions of a large historical test corpus for no production
benefit.

## Compatibility surfaces

Historical import paths remain bounded facades/aliases while callers migrate:

- `research_store.retrieval_service` aliases the canonical retrieval service
  module.
- `research_store.qdrant`, `qdrant_authority`, and
  `projection_reconciliation` alias their canonical projection modules.
- `research_store.postgres_retrieval` re-exports the canonical retrieval and
  index-job repositories.
- `research_store.retrieval` is now the capability package and exports the
  historical ranking/reranker surface.
- `research_store.retrieval.Request` and `urlopen` remain intentional package
  patch points for the historical reranker test/injection contract.

These facades contain no competing domain implementation. Removing them requires
a separately scoped caller/reference audit.

## Baseline-stable checkpoint/indexing arrangement

The checkpoint and indexing implementations intentionally remain physically at
their historical root paths. Earlier physical relocation made existing
path-keyed Pyrefly baseline debt appear as newly introduced diagnostics. The
projection package therefore exposes typed, zero-domain-logic facades while the
single implementation remains at the baseline-authoritative path.

This is not a second implementation and must not be "cleaned up" by moving the
files during ordinary #263 remediation. A future physical relocation requires a
separately reviewed type-debt/baseline migration.

## Test authority

Issue-specific structural coverage lives in
`scripts/test_issue_263_retrieval_projection_slice.py`. Isolated packaging and
canonical/legacy import identity are covered by
`scripts/test_issue_263_package_boundary.py`.

`.github/workflows/retrieval-projection-slice-review.yml` binds validation to the
exact candidate SHA, runs changed-scope Ruff/Pyrefly plus full-project Pyrefly,
and exercises focused retrieval, projection, reconciliation, checkpoint,
protection, package, and evidence regressions against disposable PostgreSQL and
Qdrant services. General merge-ref CI remains supplementary mergeability
evidence rather than a substitute for exact-head #263 validation.
