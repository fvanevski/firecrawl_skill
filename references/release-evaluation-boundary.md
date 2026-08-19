# Release and evaluation package boundary

Issue #265 establishes `research_store.release` as the canonical structural boundary for release-only benchmark, strict-campaign, evidence, validation, and benchmark CLI-assembly responsibilities.

## Before and after

| Responsibility | Before #265 | Canonical boundary after #265 | Physical implementation status |
|---|---|---|---|
| benchmark CLI assembly | `research_store.benchmark_admin` | `research_store.release.admin` | moved; legacy path is a temporary module-identity facade |
| release benchmark runner | `research_store.release_benchmark` | `research_store.release.benchmark` | staged bridge to baseline-tracked legacy implementation |
| workflow benchmark runner | `research_store.workflow_benchmark` | `research_store.release.workflow` | staged bridge to baseline-tracked legacy implementation |
| strict campaign | `research_store.strict_benchmark` | `research_store.release.strict` | staged bridge to baseline-tracked legacy implementation |
| release evidence | `research_store.release_evidence` | `research_store.release.evidence` | staged bridge to baseline-tracked legacy implementation |
| release preflight/validation | `research_store.preflight` | `research_store.release.preflight` | staged bridge to baseline-tracked legacy implementation |

The benchmark CLI now imports `research_store.release.admin`; ordinary runtime modules do not import the release package.

## Pyrefly baseline constraint

The repository Pyrefly baseline is path-keyed pre-existing debt and is read-only during normal issue work. The following release/evaluation implementations already have reviewed baseline entries:

- `scripts/research_store/release_benchmark.py`
- `scripts/research_store/workflow_benchmark.py`
- `scripts/research_store/strict_benchmark.py`
- `scripts/research_store/release_evidence.py`
- `scripts/research_store/preflight.py`

Physically relocating those files in #265 would reclassify their existing diagnostics as new diagnostics at new paths, effectively laundering the reviewed baseline. Consistent with the structural-refactor campaign precedent established by #264, #265 therefore leaves those exact implementation paths stable and gives them canonical `research_store.release.*` bridge modules. Issue #269 owns compatibility-facade removal and final cleanup once the debt-bearing implementations can move without weakening or expanding the baseline.

`benchmark_admin.py` has no such baseline constraint, so its implementation is physically relocated to `research_store.release.admin` now.

## Preservation invariants

This slice changes topology, not release semantics. It preserves:

- strict-campaign CLI arguments, exit behavior, strict-mode enforcement, and release-gate failure semantics;
- workflow/release benchmark public classes, functions, serialization behavior, and monkeypatch-visible module globals through module-identity bridges;
- release-evidence manifest schema, CI-job requirements, artifact paths, artifact hashing, fingerprint categories, and verification rules;
- preflight readiness, production-adapter, cleanup, and projection-preservation semantics;
- benchmark dataset, fixture, and golden bytes and their existing repository paths;
- the existing `pyrefly-baseline.json`, Pyrefly configuration, Ruff configuration, CI workflows, and release gates.

The final #265 diff must therefore contain no fixture/golden changes and no changes to the five baseline-tracked implementation files above.

## Runtime dependency direction

`research_store.release` may depend on ordinary runtime/domain components because release evaluation exercises them. Ordinary runtime code must not depend on `research_store.release`. The only non-release-package imports of the canonical release namespace in this staged topology are:

1. `research_store.cli.benchmark`, which is the release/benchmark CLI entry point; and
2. `research_store.benchmark_admin`, which is the temporary compatibility facade for the moved admin assembly.

`research_store.release.__init__` intentionally imports no release implementations, so importing the package alone does not eagerly acquire release infrastructure.

## Large-module review

Large legacy release/evaluation modules remain unsplit in #265 for one structural reason: their reviewed Pyrefly diagnostics are path-keyed. Splitting or relocating them in this topology-only slice would convert pre-existing debt into new unbaselined diagnostics. #269 owns removal of that staged constraint; #265 does not use file-size reduction as justification for unrelated refactoring.

## Regression evidence required before PR

The local validation handoff must demonstrate, in the repository-prescribed order:

1. Ruff check and format-check on every changed Python path;
2. Pyrefly on the explicit changed Python set, including `scripts/test_release_package_boundary.py`;
3. the focused release-package boundary test plus existing release-invariant, benchmark, strict-campaign, release-evidence, and preflight regressions;
4. full-project `pyrefly check` with the committed baseline unchanged;
5. directly affected broader release/benchmark gates; and
6. `git diff --check`.

Any service-backed regression that can reset PostgreSQL or mutate Qdrant must use `scripts/disposable-test-services` and the repository disposable-service contract. Structural/unit tests that do not touch those services require no disposable pair.
