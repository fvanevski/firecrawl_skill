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

## Exact-head and Pyrefly exit-code evidence

Validation evidence must distinguish source identity from GitHub's synthetic
pull-request merge ref. When exact-head evidence is required, record
`git rev-parse HEAD` and compare it with the authoritative task/PR head SHA.
The authoritative `Pyrefly` PR job explicitly checks out
`github.event.pull_request.head.sha`; workflow-dispatch validation uses the
requested `candidate-sha` when supplied.

Normal Ruff and Pyrefly validation commands must exit successfully. An
intentional negative-control Pyrefly probe is different: it is valid only when
Pyrefly returns diagnostic exit code `1` and the emitted diagnostic identifies
the probe file. Pyrefly exit codes `3` and `101` represent infrastructure and
internal/panic failures and must never be accepted as evidence that a negative
control worked.

## Repository merge-policy invariant

The default `main` branch must require the exact `Pyrefly` status-check context
through the effective GitHub ruleset or branch-protection policy. A successful
workflow job that is not merge-required does not satisfy the repository's
static-type merge authority.

Before final gate closure, read back the effective merge requirements for the
exact PR head and verify that `Pyrefly` is both required and successful. Do not
infer this requirement from workflow YAML, a green check list, or draft PR
state. If GitHub reports no required `Pyrefly` check, treat that as a merge-gate
failure even when the job itself succeeds.

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
- Manual baseline proposal generation may complete with Pyrefly exit code `0`
  or diagnostic exit code `1`; infrastructure/internal failures are fatal and
  must not be hidden with unconditional `continue-on-error`.

## Handoff evidence

Report the exact HEAD and the result of: changed-scope Ruff, changed-scope
Pyrefly, focused pytest, full-project Pyrefly, and relevant broader tests. A
failure that requires semantic production changes must be returned as evidence
rather than hidden by weakening typing, lint, or test configuration.
