# PR #291 test-topology review remediation

This document records the source-level remediation for issue #268 after the independent review of PR #291. It is the authority for the bounded local validation/remediation handoff, not merge-readiness evidence by itself.

## Review findings addressed centrally

### Blocking: changed-test Pyrefly authority

The reviewed head filtered `test_*.py` from changed-scope Pyrefly while full-project Pyrefly included only `scripts/**/*.py` and `src/**/*.py`. That left the relocated `tests/**/*.py` corpus outside both static-type authorities.

Central remediation restores exact ACMR changed-Python checking, **including changed tests**, in the main CI workflow and the acquisition, retrieval/projection, and assessment/reporting exact-head slice workflows. `references/local-agent-validation.md` and `tests/contract/test_pyrefly_gate.py` require the same behavior. No Pyrefly version, baseline, project scope, or suppression mechanism was weakened.

Restoring the gate exposed historical test typing debt that had previously been outside Pyrefly scope. The first restored exact-head run reported 1,475 changed-scope diagnostics. Central then declared the stable repository/method surface actually installed by `PostgresRepositoryContext.bind()` on `PostgresUnitOfWork`, rather than masking those structural errors with a permissive `__getattr__`. That reduced the next exact-head changed-scope result to 1,327 diagnostics while full-project Pyrefly remained at zero unsuppressed errors. The remaining diagnostics are test-code remediation/validation work; they are not grounds to reintroduce test exclusion or expand `pyrefly-baseline.json`.

### Blocking: behavior/boundary classification

Five PostgreSQL-backed suites were incorrectly placed under `tests/unit`. Their canonical ownership is now:

| Former path | Canonical path | Boundary evidence |
|---|---|---|
| `tests/unit/test_asset_promotion_integration.py` | `tests/integration/test_asset_promotion_integration.py` | PostgreSQL promotion-state and SQL integration |
| `tests/unit/test_asset_promotion_reopen_concurrency.py` | `tests/integration/test_asset_promotion_reopen_concurrency.py` | PostgreSQL locking/concurrency and durable recovery |
| `tests/unit/test_asset_promotion_migration_compat.py` | `tests/integration/test_asset_promotion_migration_compat.py` | real database creation plus Alembic migration compatibility |
| `tests/unit/test_curated_run_integration.py` | `tests/integration/test_curated_run_integration.py` | PostgreSQL lifecycle/provenance and lock serialization |
| `tests/unit/test_issue_215_completion_budget.py` | `tests/integration/test_issue_215_completion_budget.py` | explicitly disposable PostgreSQL-backed completion-budget behavior |

The intended active distribution is 50 unit, 53 integration, 27 contract, and 5 acceptance files: 135 tests in total. `tests/contract/test_test_topology.py` enforces that distribution, canonical ownership, and active gate/document references.

### Important non-blocking findings

The independent #291 review did not classify a separate important-non-blocking defect. Historical important-nonblocking findings documented for predecessor Phase-5 slices remain owned by their original issues and are not silently promoted into #268 scope.

### Codex Review automated suggestion

The Codex inline suggestion observed during the independent review identified an active repository-validation command in `references/run-integrity-export.md` that still targeted the legacy `scripts/` test root. The source has already been corrected to target `tests/`.

The focused review-state surface still reports that inline thread as unresolved, but it does not expose the inline body. No additional Codex requirement is inferred. The local handoff must read that exact inline body with native authenticated `gh` and verify whether current source satisfies it. If it demands anything materially different from the already-corrected test-root command, return that demand to Central rather than implementing it locally.

### Test/documentation gaps

The remediation adds explicit regression/documentation authority for both original blockers:

- `tests/contract/test_pyrefly_gate.py` requires all changed Python paths and rejects reintroduction of the test-file filter;
- `tests/contract/test_test_topology.py` locks the 50/53/27/5 active distribution, canonical integration ownership, and current consumer paths;
- active validation references consistently require changed-test Pyrefly;
- this document records the exact source-vs-local boundary and disposable-service validation contract.

Independent local exact-head execution remains deliberately outstanding; Central cannot fabricate local Git, PostgreSQL, Qdrant, concurrency, or cleanup evidence.

## Temporary deletion boundary

The focused Central GitHub content-write surface can create or replace files but cannot delete paths. The five former unit paths are therefore temporary **non-collecting tombstones**, not compatibility tests. The topology regression accepts them only when their exact relocation marker is present and the file contains no test function.

The local OpenCode agent is authorized to delete exactly these five paths, and no others, using native Git:

```bash
git rm \
  tests/unit/test_asset_promotion_integration.py \
  tests/unit/test_asset_promotion_reopen_concurrency.py \
  tests/unit/test_asset_promotion_migration_compat.py \
  tests/unit/test_curated_run_integration.py \
  tests/unit/test_issue_215_completion_budget.py
```

This deletion is mechanical cleanup of Central-write-surface artifacts. It does not authorize semantic production changes.

## Bounded test-type remediation rules

The remaining changed-test Pyrefly debt may be repaired locally only under these Central-defined rules. Anything outside them is an escalation to Central.

1. **Disposable database URL narrowing.** Where Serena confirms a test-only variable assigned by `os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")` is used only by truthiness for skip/availability and later as a `str`, change the assignment to `... or ""`. Empty string remains falsy and preserves skip semantics. Do not apply if the file distinguishes `None` from `""`.
2. **Optional database rows/results.** Replace unsafe `cursor.fetchone()[0]` or unpack/iteration on `fetchone()` with an intermediate row plus `assert row is not None`. Do not cast away optionality.
3. **Test doubles injected into concrete parameters.** Serena must verify the fake implements every member actually used. Then use `cast(ExpectedProductionType, fake)` at that injection boundary only. If behavior is missing, escalate.
4. **Intentional invalid-input/compatibility probes.** Preserve the `pytest.raises` and invalid runtime value. Cast only the deliberately invalid expression or callable at the negative-test boundary; do not weaken production signatures.
5. **Mutable container inference.** Add the narrowest correct explicit container annotation. Use `Any` only for deliberately untyped external/dynamic JSON-like records when a precise union/TypedDict is disproportionate.
6. **Optional service results.** Prove the test precondition with `assert result is not None` before dereference. Do not change the production return type for test convenience.
7. **Dynamic PostgreSQL UoW surface.** `PostgresUnitOfWork` now declares the exact members installed by `PostgresRepositoryContext.bind()`. Do not add `__getattr__` or broad UoW casts. If a reported attribute is truly installed but undeclared, return it to Central; otherwise the test is wrong.
8. **Dynamic SQL identifiers.** Use `psycopg.sql.SQL(...).format(sql.Identifier(...))`; do not cast dynamic SQL strings to `LiteralString`.
9. **Override/test-hook signatures.** Preserve compatible production parameter names/types; explicitly `del` ignored test-hook parameters when necessary rather than loosening production signatures.
10. **Structural monkeypatch attributes.** A narrowly scoped `cast(Any, object_under_test)` is allowed only for deliberate synthetic function/module attributes used by structural monkeypatch tests.

Forbidden local repairs include `# pyrefly: ignore`, `# type: ignore`, baseline expansion, Pyrefly config/version changes, changed-scope filtering, assertion/skip/xfail weakening, production behavior/signature/schema/transaction/authority changes, and unrelated refactoring.

## Local OpenCode handoff sequence

The local agent must run on the **then-current exact PR head**, not a branch approximation:

1. Native Git: `git fetch origin`; resolve PR #291 head to its exact 40-character SHA; check it out detached or in an isolated review worktree; report raw `git rev-parse HEAD`, authoritative base SHA, and complete ACMR changed-file list.
2. Serena (`no-memories`): inspect the five tombstones/canonical integration files, changed-test Pyrefly workflow contracts, and every file touched by mechanical type remediation. Audit references before and after editing.
3. Delete only the five authorized tombstones with the exact `git rm` command above.
4. Apply bounded test-only type repairs under rules 1–10 in small root-cause batches. After each batch run repository-pinned Pyrefly on the affected files. Use raw/native failure output; RTK only for routine successful output.
5. Run changed-scope Ruff `check` and `format --check --diff` over the complete exact ACMR Python set, including tests.
6. Run repository-pinned `pyrefly check <changed.py ...>` over that same complete ACMR set. Success is mandatory and does not substitute for full-project Pyrefly.
7. Run focused non-service contracts: `tests/contract/test_pyrefly_gate.py`, `tests/contract/test_test_topology.py`, `tests/contract/test_asset_promotion_contract.py`, and directly affected documentation/gate consumer tests.
8. Read `references/local-disposable-test-services.md`; choose a short unique namespace and start a fresh pair with `eval "$(scripts/disposable-test-services --namespace <namespace> up)"`. Use the emitted environment unchanged. Never use persistent PostgreSQL port `55432` or Qdrant port `6333`; if defaults are occupied, pass unused non-protected ports through the helper.
9. Run the five canonical PostgreSQL integration suites and the affected checkpoint/authoritative-fsearch/release-gate/service-backed authorities. Use `reset-qdrant` before point-count/reconciliation/orphan evidence when prior Qdrant contents could matter.
10. Run full-project `pyrefly check` with no file arguments and report it separately.
11. Run relevant broader Python 3.11/3.12 pytest/contract/integration authorities justified by #268.
12. Run helper `down` and verify no helper-owned containers remain. Report namespace, selected endpoints/ports, whether `reset-qdrant` was used, and cleanup result.
13. Read the exact unresolved Codex inline thread with native authenticated `gh`; verify current source satisfies it or escalate a materially different requirement.
14. Report final raw `git diff --check`, `git status --short`, and `git rev-parse HEAD`.

The local agent may commit/push **only** the five authorized tombstone deletions and mechanical test-only type repairs produced under rules 1–10, in one narrowly scoped remediation commit on `refactor/test-topology`, and only after the focused static/runtime checks for those edits pass. It must not commit a production change. Report the resulting 40-character SHA to Central.

## Central closure after local handoff

After that local commit/push, Central must re-fetch PR #291, inspect the complete exact-head diff, independently audit the local mechanical diff against rules 1–10, re-read #268 and review/Codex state, require separate successful exact-head Ruff/changed-scope Pyrefly/full-project Pyrefly/applicable runtime authorities, re-check merge policy/review state, and only then choose a new formal review disposition.

No merge is authorized by this document.
