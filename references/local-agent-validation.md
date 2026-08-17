# Local AI agent validation contract

This repository uses the same static-analysis authorities locally and in CI:
Ruff for lint/format policy and Pyrefly for Python type correctness. Pyrefly is
pinned in `requirements-typecheck.txt`; do not substitute mypy or ty as a merge
authority.

## Completion sequence

For substantive Python changes, the local agent must complete validation in
this order before handoff:

1. **Ruff on changed Python code.** Determine the Python files changed from the
   task's authoritative base and run `ruff check` on that bounded set. Apply
   `ruff format --check` to the same set when formatting could have changed.
2. **Pyrefly on changed Python scope.** Run `pyrefly check <changed.py ...>` so
   interface/type errors are surfaced while the edit context is still narrow.
3. **Focused pytest.** Run the smallest deterministic unit/contract/integration
   tests that exercise the behavior changed by the task. PostgreSQL/Qdrant or
   other service-backed tests must use disposable local services.
4. **Full-project Pyrefly.** Run `pyrefly check` with no file arguments before
   handoff. This is mandatory even if the changed-scope check passed because a
   local interface change can break callers outside the edited files.
5. **Relevant broader tests.** Run the repository suites/gates appropriate to
   the affected subsystem and task acceptance criteria. Do not replace focused
   tests with the broad suite; both have different diagnostic value.

A typical changed-file setup is:

```bash
BASE_REF="${BASE_REF:-origin/main}"
mapfile -t CHANGED_PY < <(
  git diff --name-only --diff-filter=ACMR "$BASE_REF"...HEAD -- '*.py'
)

if ((${#CHANGED_PY[@]})); then
  ruff check "${CHANGED_PY[@]}"
  ruff format --check --diff "${CHANGED_PY[@]}"
  pyrefly check "${CHANGED_PY[@]}"
fi

# After focused tests:
pyrefly check
```

For an uncommitted working tree, include staged/unstaged Python paths in the
bounded changed-file set rather than relying only on `BASE_REF...HEAD`.

## Baseline policy

`pyrefly-baseline.json` records typing debt that existed when Pyrefly became an
authoritative gate. It is not a general suppression mechanism.

- Normal local validation and CI **read** the baseline; they never update it.
- A new diagnostic is a failure and should normally be fixed in the task that
  introduced it.
- Do not mass-add `# pyrefly: ignore` comments to bypass the gate.
- Baseline reductions are welcome when nearby code is made type-clean.
- Baseline additions or Pyrefly-version upgrades require a deliberate,
  separately reviewed tooling change with an explanation of every new debt
  class being accepted.

## Handoff evidence

Report the exact HEAD and the result of: changed-scope Ruff, changed-scope
Pyrefly, focused pytest, full-project Pyrefly, and relevant broader tests. A
failure that requires semantic production changes must be returned as evidence
rather than hidden by weakening typing, lint, or test configuration.
