# Issue #269 final compatibility-cleanup contract

This is the implementation and handoff ledger for the final compatibility-removal phase. Architecture decisions in this document are Central-owned; a local agent may execute only the listed mechanical removals, exact caller rewrites already specified here, formatting, baseline pruning, and validation after independently confirming the exact PR head.

## Final package rule

Production code is installed exclusively from `src/firecrawl_skill`. `scripts/` may contain executable/operator tooling and test/fixture support, but it is not a setuptools production-module root. A Python file under `scripts/` that contains ordinary domain/application implementation must either move to a named canonical owner under `src/firecrawl_skill` or be deleted when its canonical copy exists.

## Canonical owners and required retirements

| Legacy/migration path | Final owner | Final action |
|---|---|---|
| `scripts/budget_policy.py` | `research_store/budget_policy.py` | delete after caller migration |
| `scripts/candidate_ranking.py` | `research_store/acquisition/candidate_ranking.py` | delete after caller migration |
| `scripts/classifier.py` | `research_store/acquisition/classifier.py` | delete after caller migration |
| `scripts/model_gateway.py` | `firecrawl_skill/model_gateway.py` | migrate fixtures/workflows, then delete script copy |
| `scripts/drain_index_jobs.py` | `research_store/retrieval/projection/drain.py` | retain only as executable operator launcher; never package as `py-module` |
| `research_store/service.py` | `corpus_service`, `assessment.*`, `export_serialization` | migrate imports, then delete generic aggregation |
| `coverage_service.py` | `assessment/coverage.py` | migrate callers, delete facade |
| `quality_service.py` | `assessment/quality.py` | migrate callers, delete facade |
| `duplicate_service.py` | `assessment/duplicates.py` | migrate callers, delete facade |
| `evidence_grouping.py` | `assessment/grouping.py` | migrate callers, delete facade |
| `audit_packet.py` | `assessment/audit_packet.py` | migrate callers, delete facade |
| `evidence.py` | `assessment/evidence.py` | implementation moved; migrate callers and delete old implementation |
| `claim_binding_service.py` | `assessment/binding.py` | implementation moved; migrate callers and delete old path |
| `packet_validator.py` | `assessment/validation.py` | implementation moved; migrate callers and delete old implementation |
| `report_service.py` | `reporting/construction.py` | move implementation with package-relative imports/schema-root correction, migrate callers, delete old implementation |
| `report_validator.py` | `reporting/validation.py` | implementation moved; migrate callers and delete old implementation |
| `report_artifact_service.py` | `reporting/artifacts.py` | migrate callers, delete facade |
| `acquisition_service.py` | `acquisition/service.py` + `acquisition/adapters/bounded_firecrawl.py` | migrate callers, delete facade |
| `release_benchmark.py` | `release/benchmark.py` | migrate callers, delete facade |
| `research_store/cli.py` | `research_store/cli/` package | delete same-name direct-launch facade after caller audit |
| root retrieval/projection facades enumerated in `retrieval-projection-boundary.md` | `research_store/retrieval/**` | delete after caller audit |

`research_store/postgres_audit.py` is a justified retention: it is a substantive connection-bound persistence repository in the established `postgres_*` infrastructure layer, not a compatibility facade.

A retained compatibility path requires explicit evidence of a supported external contract. Repository-internal historical imports do not justify retention; they must migrate.

## Exact mechanical caller rewrites

The following replacements are architecture-fixed and may be executed mechanically after exact-head reference census:

- `.service import dumps` -> `.export_serialization import dumps`;
- `.service import json_default` -> `.export_serialization import json_default`;
- `..service import dumps/json_default` from `research_store.cli` -> `..export_serialization import dumps/json_default`;
- `from budget_policy import ...` inside installed `firecrawl_skill.*` code -> the capability-relative `research_store.budget_policy` import;
- imports of flat assessment/reporting/acquisition/release facades -> the canonical owner named in the table above;
- imports of top-level `model_gateway`/`scripts.model_gateway` from production or tests -> `firecrawl_skill.model_gateway`;
- workflow Pyrefly targets for model gateway -> `src/firecrawl_skill/model_gateway.py` plus any genuine fixture file;
- tests that import a removed facade only to prove identity -> import the canonical owner and assert the legacy path is physically absent.

If a reference census exposes a caller for which the replacement is not uniquely determined by this table, stop and return that caller to Central. Do not add a new compatibility facade.

## Report construction move

`report_service.py` must be moved without behavioral redesign into `reporting/construction.py`. Because the destination is one package level deeper, all research-store sibling imports become parent-relative (`..authorized_semantic`, `..completion_provenance`, `..config`, `..domain`, `..semantic_cache`, `..semantic_service`, `..telemetry_service`); EvidenceService comes from `..assessment.evidence`; ClaimBindingService from `..assessment.binding`; ReportArtifactService from `.artifacts`. Schema discovery must continue to resolve the repository `schemas/research-workflow` directory from the deeper destination rather than relying on the old `__file__` depth.

The final structural test requires `LocalSynthesisService.__module__ == firecrawl_skill.research_store.reporting.construction` and physical absence of `report_service.py`.

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

After current-source reference analysis shows no unsupported caller, physically remove:

- `scripts/budget_policy.py`
- `scripts/candidate_ranking.py`
- `scripts/classifier.py`
- `scripts/model_gateway.py`
- `src/firecrawl_skill/research_store/service.py`
- `src/firecrawl_skill/research_store/coverage_service.py`
- `src/firecrawl_skill/research_store/quality_service.py`
- `src/firecrawl_skill/research_store/duplicate_service.py`
- `src/firecrawl_skill/research_store/evidence_grouping.py`
- `src/firecrawl_skill/research_store/audit_packet.py`
- `src/firecrawl_skill/research_store/evidence.py`
- `src/firecrawl_skill/research_store/claim_binding_service.py`
- `src/firecrawl_skill/research_store/packet_validator.py`
- `src/firecrawl_skill/research_store/report_service.py`
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

The local agent must not add replacement facades when a deletion exposes an internal caller; it must report that caller for Central-directed migration.

## Automated/Codex review class coverage

No Codex-authored review, inline thread, conversation comment, or Codex-named check payload is retrievable from the authoritative PR surfaces inspected for #292. Therefore there is no authoritative unseen suggestion text to claim resolved.

The reproducible automated-review classes are nevertheless covered by explicit final contracts: import-context assumptions (canonical package-only imports), wheel/package completeness (isolated wheel), alias/facade identity (replaced by physical-absence assertions), missing exact-head execution (required evidence list below), and undocumented Pyrefly-baseline movement (explicit migration rule above). If a Codex suggestion becomes visible later, capture its immutable review/comment identity and exact head before changing code; do not infer content from labels or UI state.

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
