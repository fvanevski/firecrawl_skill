# Release and evaluation package boundary

Issue #265 establishes `research_store.release` as the canonical physical and structural boundary for release-only benchmark, strict-campaign, evidence, validation, and benchmark CLI-assembly responsibilities.

## Before and after

| Responsibility | Before #265 | Canonical implementation after #265 | Legacy compatibility surface |
|---|---|---|---|
| benchmark CLI assembly | `research_store.benchmark_admin` | `research_store.release.admin` | `research_store.benchmark_admin` |
| measured release benchmark | `research_store.release_benchmark` | `research_store.release.benchmark` | `research_store.release_benchmark` |
| workflow benchmark | `research_store.workflow_benchmark` | `research_store.release.workflow` | `research_store.workflow_benchmark` |
| strict campaign | `research_store.strict_benchmark` | `research_store.release.strict` | `research_store.strict_benchmark` |
| release evidence | `research_store.release_evidence` | `research_store.release.evidence` | `research_store.release_evidence` |
| release preflight/validation | `research_store.preflight` | `research_store.release.preflight` | `research_store.preflight` |

The flat compatibility modules contain no functions or classes. They preserve module identity and supported historical import/monkeypatch surfaces while all release/evaluation domain logic is owned by `research_store.release`. Issue #269 owns caller-audited removal of those facades.

## Pyrefly debt migration

The five historical implementation paths carried path-keyed entries in `pyrefly-baseline.json`. Moving an implementation cannot justify copying those suppressions to its canonical path. The #265 remediation therefore treats physical relocation as an explicit type-debt migration:

- canonical `src/firecrawl_skill/research_store/release/*.py` implementation paths must pass repository-pinned Pyrefly without new baseline entries;
- `pyrefly-baseline.json` is not expanded;
- no broad inline suppressions, scope/config weakening, or checker-version change is permitted to make the move green;
- historical entries for legacy paths are not authority for canonical code and may be removed only when their corresponding compatibility surfaces are retired/audited as appropriate.

The changed-scope CI gate checks the actual ACMR Python paths from the exact PR base/head, including changed tests explicitly, in addition to the independent full-project Pyrefly gate.

## Relocation-sensitive authority paths

Physical relocation changes `__file__` depth and release-source artifact identity. The canonical implementation therefore binds these paths explicitly:

- strict-campaign repository root, default benchmark dataset and dependency-lock hashing resolve from repository root rather than the historical flat-module depth;
- preflight Alembic configuration resolves from repository root after its package move;
- release-evidence artifact hashing and exact-head evidence classification use `release/benchmark.py` and `release/workflow.py`, not the compatibility facades.

These are topology corrections only; release policy, schemas, CLI semantics, provenance, and service authority remain unchanged.

## Runtime dependency direction

`research_store.release` may depend on ordinary runtime/domain components because release evaluation exercises them. Ordinary runtime code must not acquire release infrastructure. Outside `research_store.release`, canonical release imports are limited to:

1. `research_store.cli.benchmark`, the benchmark/release CLI entry point; and
2. temporary flat compatibility facades required by supported callers.

`research_store.release.__init__` imports no implementation modules, so importing the package alone does not eagerly acquire release infrastructure.

The architecture regression parses absolute and relative import forms, including `from . import release` and `from .. import release`, and fails on any unapproved runtime coupling.

## Contract preservation

This slice changes topology, not release semantics. It preserves:

- strict-campaign CLI arguments, exit behavior, strict-mode enforcement, preflight and release-gate failure semantics;
- workflow/release benchmark public classes, functions, serialization behavior and supported monkeypatch-visible module globals;
- release-evidence manifest schema, CI-job requirements, artifact hashing, fingerprint categories and verification rules;
- preflight readiness, production-adapter, cleanup and projection-preservation semantics;
- benchmark dataset, fixture and golden provenance;
- existing release, benchmark, lint, type and CI authorities.

No fixture or golden-data byte change is required by the relocation.

## Large-module review

The measured release benchmark remains a large canonical module because it is one release-policy capability whose metric extraction, provenance/status semantics, recommendation policy and reproducibility rules are jointly constrained by the release-invariant suite. Arbitrary file-count or LOC splitting during a topology remediation would enlarge the semantic diff and transaction/provenance review surface. The module is therefore retained intact as one capability boundary for #265; future decomposition requires its own invariant-preserving issue rather than an LOC-only split.

The other release modules remain capability-oriented: workflow benchmark execution, strict campaign orchestration, release evidence, and preflight validation. No module is retained at a legacy path merely to preserve Pyrefly debt.

## Required exact-head validation

Before local evidence is returned, validation must run in this order:

1. Ruff `check` and `format --check --diff` on exact ACMR changed Python paths;
2. repository-pinned Pyrefly on the same explicit changed Python set, including changed tests such as `tests/contract/test_release_package_boundary.py`;
3. focused boundary, release-invariant, benchmark, strict-campaign, release-evidence and preflight regressions;
4. full-project `pyrefly check` with the committed baseline/config unchanged;
5. directly affected broader capability/contract/integration authorities;
6. `git diff --check` and exact-head identity readback.

Any test that can reset PostgreSQL or mutate Qdrant projections must use `scripts/disposable-test-services`. Persistent PostgreSQL port `55432` and Qdrant port `6333` are never review/remediation test targets.
