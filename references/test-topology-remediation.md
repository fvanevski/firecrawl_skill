# PR #291 test-topology review remediation

This document records the source-level remediation for issue #268 after the independent review of PR #291. It is an implementation record and local-validation handoff, not merge-readiness evidence.

## Review findings addressed

### Changed-test Pyrefly authority

The reviewed head filtered `test_*.py` from changed-scope Pyrefly while full-project Pyrefly included only `scripts/**/*.py` and `src/**/*.py`. That left the relocated `tests/**/*.py` corpus outside both static-type authorities.

The remediation restores exact ACMR changed-Python checking, including changed tests, in the main CI workflow and the acquisition, retrieval/projection, and assessment/reporting exact-head slice workflows. `references/local-agent-validation.md` and the Pyrefly gate regression require the same behavior. No Pyrefly version, baseline, project scope, or suppression mechanism was weakened.

Restoring the gate intentionally exposed historical test typing debt. Those diagnostics are implementation work: they must be resolved in test code or represented with narrowly typed casts where a test deliberately supplies a test double or invalid runtime value. They are not grounds to reintroduce test exclusion or expand `pyrefly-baseline.json`.

### Behavior/boundary classification

Five PostgreSQL-backed suites were incorrectly placed under `tests/unit`. Their canonical ownership is now:

| Former path | Canonical path | Boundary evidence |
|---|---|---|
| `tests/unit/test_asset_promotion_integration.py` | `tests/integration/test_asset_promotion_integration.py` | PostgreSQL promotion-state and SQL integration |
| `tests/unit/test_asset_promotion_reopen_concurrency.py` | `tests/integration/test_asset_promotion_reopen_concurrency.py` | PostgreSQL locking/concurrency and durable recovery |
| `tests/unit/test_asset_promotion_migration_compat.py` | `tests/integration/test_asset_promotion_migration_compat.py` | real database creation plus Alembic migration compatibility |
| `tests/unit/test_curated_run_integration.py` | `tests/integration/test_curated_run_integration.py` | PostgreSQL lifecycle/provenance and lock serialization |
| `tests/unit/test_issue_215_completion_budget.py` | `tests/integration/test_issue_215_completion_budget.py` | explicitly disposable PostgreSQL-backed completion-budget behavior |

The intended active distribution is therefore 50 unit, 53 integration, 27 contract, and 5 acceptance files: 135 tests in total. `tests/contract/test_test_topology.py` enforces that distribution, canonical ownership, and active gate/document references.

## Temporary deletion boundary

The focused Central GitHub content-write surface can create or replace files but cannot delete paths. The five former unit paths are therefore temporary **non-collecting tombstones**, not compatibility tests. The topology regression ignores them only when their exact relocation marker is present and contains no test function.

Before final local validation, the local OpenCode agent must perform this narrowly prescribed mechanical deletion with native Git:

```bash
git rm \
  tests/unit/test_asset_promotion_integration.py \
  tests/unit/test_asset_promotion_reopen_concurrency.py \
  tests/unit/test_asset_promotion_migration_compat.py \
  tests/unit/test_curated_run_integration.py \
  tests/unit/test_issue_215_completion_budget.py
```

This is the only source-tree mutation delegated by this remediation. It removes Central-write-surface artifacts; it must not be used as authorization for semantic test or production edits.

## Codex review suggestion

The Codex inline suggestion concerning the active repository-validation command in `references/run-integrity-export.md` has already been incorporated: the current command targets `tests/`, not the legacy `scripts/` test root. Local handoff should read the exact thread body and confirm semantic satisfaction; no further source change is expected unless the thread contains a materially different demand.

## Local exact-head validation

After fetching the then-current PR head and performing the five `git rm` operations above, the local agent must use Serena with `no-memories` for semantic inspection, RTK for routine successful output, OpenViking only for bounded historical rationale, and native/raw commands for exact identity, failures, service state, transaction/concurrency evidence, and cleanup.

Validation order:

1. report raw `git rev-parse HEAD`, the authoritative base SHA, and complete ACMR changed-file list;
2. run `ruff check` and `ruff format --check --diff` over all changed Python paths;
3. run repository-pinned `pyrefly check <changed.py ...>` over the same changed Python paths, including tests;
4. run focused topology/Pyrefly contracts;
5. start a fresh disposable PostgreSQL/Qdrant pair with `scripts/disposable-test-services` using a unique namespace;
6. run the five canonical integration suites plus checkpoint, authoritative-fsearch, release-gate, and other affected service-backed authorities;
7. use `reset-qdrant` before point-count/reconciliation evidence if prior Qdrant contents could matter;
8. run full-project `pyrefly check` separately;
9. run the relevant broader Python 3.11/3.12/contract/integration families;
10. run helper `down` and verify no helper-owned containers remain.

Any failure is evidence to return to Central. Do not alter production code, tests, Pyrefly configuration/baseline, CI authority, or release gates merely to make validation pass.
