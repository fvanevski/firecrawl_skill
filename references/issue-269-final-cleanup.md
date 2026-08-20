# Issue #269 final compatibility-cleanup contract

This is the implementation and handoff ledger for the final compatibility-removal phase. Architecture decisions in this document are Central-owned; a local agent may execute only the listed mechanical removals, formatting, baseline pruning, and validation after independently confirming the exact PR head.

## Final package rule

Production code is installed exclusively from `src/firecrawl_skill`. `scripts/` may contain executable/operator tooling and test/fixture support, but it is not a setuptools production-module root. A Python file under `scripts/` that contains ordinary domain/application implementation must either move to a named canonical owner under `src/firecrawl_skill` or be deleted when its canonical copy exists.

## Canonical owners and required retirements

| Legacy/migration path | Final owner | Final action |
|---|---|---|
| `scripts/budget_policy.py` | `research_store/budget_policy.py` | delete after caller audit/migration |
| `scripts/candidate_ranking.py` | `research_store/acquisition/candidate_ranking.py` | delete after caller audit/migration |
| `scripts/classifier.py` | `research_store/acquisition/classifier.py` | delete after caller audit/migration |
| `scripts/model_gateway.py` | `firecrawl_skill/model_gateway.py` | move implementation, migrate callers, delete script copy |
| `scripts/drain_index_jobs.py` | `research_store/retrieval/projection/drain.py` | retain only as executable operator launcher; never package as `py-module` |
| `research_store/service.py` | `corpus_service`, `assessment.*`, `export_serialization` | migrate imports, then delete generic aggregation |
| `research_store/report_validator.py` | `research_store/reporting/validation.py` | move implementation, migrate callers, delete root facade/owner |
| `research_store/report_artifact_service.py` | `research_store/reporting/artifacts.py` | migrate callers, delete facade |
| `research_store/acquisition_service.py` | `research_store/acquisition.service` | migrate callers, delete facade |
| `research_store/release_benchmark.py` | `research_store/release.*` canonical owner | migrate callers, delete facade |
| `research_store/cli.py` | `research_store/cli/` package | delete same-name launch facade after caller audit |
| root retrieval/projection facades enumerated in `retrieval-projection-boundary.md` | `research_store/retrieval/**` | delete after caller audit |
| `research_store/claim_binding_service.py` | `research_store/assessment/binding.py` | migrate callers, delete facade |

A retained compatibility path requires explicit evidence of a supported external contract. Repository-internal historical imports do not justify retention; they must migrate.

## Serialization owner

`research_store/export_serialization.py` owns `json_default`, historical indented `dumps`, canonical export JSON, and atomic export writes. `service.py` must not remain merely to supply JSON helpers.

## Reporting owner

`research_store/reporting/validation.py` is the deterministic report-validation implementation owner. `ReportValidationSeverity` is a typed string enum (`str, Enum`), not an untyped string-constant pseudo-enum. `reporting/artifacts.py` consumes this owner directly.

## Model gateway owner

Provider transport/response normalization belongs at `firecrawl_skill.model_gateway`. It may depend on `research_store.semantic_service` for deterministic redaction/schema validation, but `research_store` production code must not depend on an ordinary implementation under `scripts/`. Test fixtures may remain under `scripts/fixtures` only when they are clearly fixtures and not packaged runtime code.

## Pyrefly migration rule

1. Keep the checker version/config/scope fixed during cleanup.
2. Run Pyrefly on the exact changed Python paths and then on the full configured project.
3. Fix diagnostics created by moved/canonical code rather than adding baseline suppressions.
4. After obsolete files are physically removed, delete baseline records whose path no longer exists.
5. Compare normalized `(diagnostic kind, message, source identity)` debt with the pre-cleanup baseline. No new diagnostic identity may be hidden by a path-only re-key.
6. Do not regenerate the baseline wholesale.

## Mechanical deletion manifest

After current-source reference analysis shows no supported caller, physically remove:

- `scripts/budget_policy.py`
- `scripts/candidate_ranking.py`
- `scripts/classifier.py`
- `scripts/model_gateway.py`
- `src/firecrawl_skill/research_store/service.py`
- `src/firecrawl_skill/research_store/report_validator.py`
- `src/firecrawl_skill/research_store/report_artifact_service.py`
- `src/firecrawl_skill/research_store/acquisition_service.py`
- `src/firecrawl_skill/research_store/release_benchmark.py`
- `src/firecrawl_skill/research_store/cli.py`
- `src/firecrawl_skill/research_store/retrieval.py`
- `src/firecrawl_skill/research_store/retrieval_core.py`
- `src/firecrawl_skill/research_store/retrieval_service.py`
- `src/firecrawl_skill/research_store/postgres_retrieval.py`
- `src/firecrawl_skill/research_store/qdrant.py`
- `src/firecrawl_skill/research_store/qdrant_authority.py`
- `src/firecrawl_skill/research_store/projection_reconciliation.py`
- `src/firecrawl_skill/research_store/indexing.py`
- `src/firecrawl_skill/research_store/checkpoint_indexing_stage.py`
- `src/firecrawl_skill/research_store/index_checkpoint_asset_membership.py`
- `src/firecrawl_skill/research_store/index_checkpoint_core.py`
- `src/firecrawl_skill/research_store/index_checkpoint_finalize.py`
- `src/firecrawl_skill/research_store/index_checkpoint_models.py`
- `src/firecrawl_skill/research_store/index_checkpoint_replay.py`
- `src/firecrawl_skill/research_store/index_checkpoint_service.py`
- `src/firecrawl_skill/research_store/index_checkpoint_store.py`
- `src/firecrawl_skill/research_store/claim_binding_service.py`

The local agent must not add replacement facades when a deletion exposes an internal caller; it must report that caller for Central-directed migration.

## Codex Review automated suggestions

As of the source/review/check surfaces inspected for PR #292, no Codex-authored review, inline thread, conversation comment, or Codex-named check payload is retrievable. Therefore there is no authoritative Codex suggestion text to implement or mark resolved. If a Codex suggestion becomes visible later, capture its immutable review/comment identity and exact head before changing code; do not infer content from labels or UI state.

## Required final evidence

- raw exact 40-character PR head and unchanged base;
- complete changed-file census;
- current-source reference census for every deleted facade/delegate;
- `ruff check` and `ruff format --check` on changed Python plus repository gate;
- changed-scope Pyrefly and full-project Pyrefly, with before/after baseline comparison and no scope/config weakening;
- focused package, retrieval/projection, checkpoint, acquisition, assessment/reporting, orchestration, release, fscrape/fsearch, and CLI contracts;
- disposable PostgreSQL/Qdrant runs for mutation/reset tests, including reset and teardown evidence;
- isolated-wheel import/package-content proof;
- exact-head GitHub CI re-read after the final push.
