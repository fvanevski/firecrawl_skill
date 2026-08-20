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
- `pyrefly-baseline.json` is pre-existing debt authority only. #263 does not
  expand it, add broad suppressions, or change Pyrefly scope/version to make a
  structural move appear clean.

## Boundary map

| Responsibility | Canonical namespace | Current implementation contract |
|---|---|---|
| Ranking/RRF/context packing/reranking | `research_store.retrieval.ranking` | Retrieval application support; preserves historical reranker patch hooks. |
| Retrieval orchestration | `research_store.retrieval.service` | PostgreSQL lexical retrieval plus optional derived Qdrant dense retrieval; preserves degradation/provenance and hard bounds. |
| Retrieval provenance persistence | `research_store.retrieval.postgres` | Connection-bound PostgreSQL repository; no transaction ownership. |
| Qdrant projection client | `research_store.retrieval.projection.qdrant` | Rebuildable dense-vector projection only. |
| Cross-store alias authority | `research_store.retrieval.projection.authority` | Compares PostgreSQL-active definition with the required Qdrant alias; PostgreSQL lifecycle remains authoritative. |
| Projection reconciliation | `research_store.retrieval.projection.reconciliation` | Observes/repairs current derived projection under existing safeguards. |
| Durable index-job repository | `research_store.retrieval.projection.postgres_jobs` | PostgreSQL-authoritative job/lease/completion persistence. |
| Index worker/embedder | `research_store.retrieval.projection.indexing` | Canonical projection namespace over one baseline-stable historical implementation pending #269 physical cleanup. |
| Checkpoint family | `research_store.retrieval.projection.index_checkpoint_*` and `checkpoint_indexing_stage` | Canonical projection namespace over one baseline-stable historical implementation family pending #269 physical cleanup. |

## Supported import contexts

There are two supported source identities during the package-boundary migration:

1. Canonical source/wheel imports use `firecrawl_skill.research_store.*`.
2. The historical pytest corpus inserts `scripts/` first in `scripts/conftest.py`,
   so direct source tests may load the retained implementation as
   `research_store.*`.

These contexts intentionally execute the same repository-owned implementation.
Structural tests assert the exact capability-relative module rather than assuming
that the optional `firecrawl_skill.` prefix is present. Installed-package tests
remain strict: canonical wheel imports expose canonical
`firecrawl_skill.research_store.*` identity and legacy imports resolve to the
same module objects.

Changing global pytest `sys.path` ordering is not part of #263. Doing so would
alter the import assumptions of a large unrelated historical test corpus for no
production benefit.

## Compatibility and migration surfaces

Historical imports remain bounded while callers migrate:

- `research_store.retrieval_service` aliases the canonical retrieval service.
- `research_store.qdrant`, `qdrant_authority`, and
  `projection_reconciliation` alias canonical projection modules.
- `research_store.postgres_retrieval` re-exports canonical retrieval and
  index-job repositories.
- `research_store.retrieval` is the capability package and exports the
  historical ranking/reranker surface.
- `research_store.retrieval.Request` and `urlopen` remain intentional package
  patch points for the historical reranker test/injection contract.

Two sibling source files require a more precise classification:

- `src/firecrawl_skill/research_store/retrieval.py`
- `src/firecrawl_skill/research_store/retrieval_core.py`

They are **not** authoritative runtime import boundaries after
`research_store.retrieval` became a package. They contain no domain definitions
and are retained only as migration residue until the campaign's destructive
compatibility cleanup. No new caller may depend on either file. Issue #269 owns
their reference-audited deletion after the remaining package migration is
complete. The #263 structural test fails if either file regains a class or
function implementation.

This staged disposition is required by #269's campaign invariant: compatibility
behavior must not be removed until current-source reference analysis demonstrates
that supported callers have migrated. #263 therefore must not perform an
opportunistic deletion merely to make the tree look cleaner.

## Baseline-stable checkpoint/indexing arrangement

The projection namespace is authoritative for ownership, but the index worker and
checkpoint implementation bodies intentionally remain physically at their
historical root paths for this phase. The projection modules are zero-domain-logic
facades over those single implementations.

This is an explicitly temporary locality exception, not the final architecture.
The reason is evidence-based: the repository's path-keyed Pyrefly baseline
contains historical debt in `indexing.py`, `checkpoint_indexing_stage.py`, and
checkpoint mixin files. A mechanical physical move would reclassify that
historical debt as new diagnostics unless the debt is actually resolved or
migrated under review.

The final physical relocation belongs to #269 together with compatibility
removal. That work must:

1. perform current-source reference analysis;
2. move the single implementations into the final canonical package;
3. resolve or explicitly review affected type debt without expanding the
   baseline or weakening Pyrefly;
4. remove stale historical baseline entries made obsolete by the move;
5. retire the temporary facades after callers are migrated; and
6. rerun exact-head Ruff, changed-scope/full-project Pyrefly, package, checkpoint,
   PostgreSQL, Qdrant, reconciliation, and protection authorities.

Until then, the staged facades are required to remain zero-domain-logic and
identity-equivalent to the historical implementation. This requirement is tested
both from the source tree and from an isolated built wheel.

## Test authority

Issue-specific structural coverage lives in
`tests/unit/test_issue_263_retrieval_projection_slice.py`. It verifies:

- canonical retrieval/projection ownership;
- compatibility identity;
- that migration-only `retrieval.py`/`retrieval_core.py` contain no domain
  definitions; and
- that the complete checkpoint projection facade family contains no competing
  domain implementation.

Isolated packaging is covered by
`tests/contract/test_issue_263_package_boundary.py`. The wheel regression enumerates and
imports every newly added retrieval/projection module, including all checkpoint
facades, and verifies facade-to-root object identity for the staged
indexing/checkpoint implementation family.

`.github/workflows/retrieval-projection-slice-review.yml` binds validation to the
exact candidate SHA, runs changed-scope Ruff/Pyrefly plus full-project Pyrefly,
and exercises focused retrieval, projection, reconciliation, checkpoint,
protection, package, and evidence regressions against disposable PostgreSQL and
Qdrant services. General merge-ref CI remains supplementary evidence rather than
a substitute for exact-head #263 validation.

## Review-finding disposition

The independent review of PR #285 found no blocking implementation defect.
Its two architecture observations are now explicitly classified as mandatory
campaign follow-up under #269 rather than ambiguous #263 cleanup:

- shadowed retrieval migration files require reference-audited deletion; and
- staged indexing/checkpoint facades require a separately reviewed physical
  relocation/type-debt migration.

The concrete #263 test gap—lack of full isolated-wheel coverage for the checkpoint
facade family—is closed in this phase. Current GitHub review/conversation
surfaces expose no Codex Review automated suggestion text for PR #285; no unseen
suggestion is treated as evidence or pre-resolved.
