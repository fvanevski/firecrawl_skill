# Local AI agent validation contract

For exact-SHA host-assessment profiles, use the deterministic control plane in
`references/local-agent-assessment.md` and `scripts/local-agent-assessment`.
Its result is `HOST_EVIDENCE_RESULT`; every report keeps
`GATE_DECISION=NOT_EVALUATED` because Central retains architectural and gate
authority. The manual sequence below remains applicable only to validation
scopes not yet represented by a reviewed assessment profile.

This repository uses the same static-analysis authorities locally and in CI:
Ruff for lint/format policy and Pyrefly for Python type correctness. Pyrefly is
pinned in `requirements-typecheck.txt`; do not substitute mypy or ty as a merge
authority.

## Exact-head checkout comes first

For PR review or remediation validation, source identity is an invariant, not
metadata. The local agent must use native Git to fetch and detach at the exact
40-character PR head supplied by Central before semantic inspection or test
execution. Never validate an approximate branch name or GitHub's synthetic PR
merge ref when an exact head is available.

```bash
git fetch origin

git checkout --detach "$REVIEW_HEAD_SHA"
test "$(git rev-parse HEAD)" = "$REVIEW_HEAD_SHA"
git cat-file -e "$BASE_SHA^{commit}"

git rev-parse HEAD
git diff --name-status --find-renames "$BASE_SHA...$REVIEW_HEAD_SHA"
```

Record the raw `git rev-parse HEAD`, the exact base SHA, and the complete
changed-file list before returning evidence. If the requested head cannot be
resolved exactly, stop validation and report the identity failure rather than
falling back to a branch tip or merge ref.

For local-agent tooling, use Serena first for symbols/references/dependency
inspection after the exact checkout is established. RTK may compress routine
successful Ruff/Pyrefly/pytest output where it preserves the decisive result.
Use native/raw Git for exact SHAs and complete diffs, and raw native command
output for failures, database/runtime evidence, transaction/concurrency
findings, release/security conclusions, or any case where filtering could hide
decisive evidence. OpenViking may supply bounded historical rationale only; it
never overrides current source, Git, CI, database, or runtime state.

## Completion sequence

For substantive Python changes, the local agent must complete validation in
this order before handoff:

1. **Ruff on changed Python code.** Determine the existing added/copied/
   modified/renamed Python files from the task's authoritative base and run
   `ruff check` plus `ruff format --check --diff` on that explicit set.
2. **Pyrefly on changed Python scope.** Run `pyrefly check <changed.py ...>` so
   interface/type errors are surfaced while the edit context is still narrow.
   Include changed test files explicitly even when project defaults exclude the
   historical test corpus.
3. **Focused pytest.** Run the smallest deterministic unit/contract/integration
   tests that can falsify the changed behavior. PostgreSQL/Qdrant or other
   service-backed tests must use disposable local services.
4. **Full-project Pyrefly.** Run `pyrefly check` with no file arguments before
   handoff. This is mandatory even if the changed-scope check passed because a
   local interface change can break callers outside the edited files.
5. **Relevant broader tests.** Run the repository suites/gates appropriate to
   the affected subsystem and task acceptance criteria. Do not replace focused
   tests with the broad suite; both have different diagnostic value.

A typical changed-file setup is:

```bash
mapfile -t CHANGED_PY < <(
  git diff --name-only --diff-filter=ACMR \
    "$BASE_SHA...$REVIEW_HEAD_SHA" -- '*.py'
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
bounded changed-file set rather than relying only on `BASE_SHA...HEAD`.

## Exact-head and Pyrefly exit-code evidence

Validation evidence must distinguish source identity from GitHub's synthetic
pull-request merge ref. A workflow is exact-head evidence only when it checks
out the immutable PR head/candidate SHA and asserts `git rev-parse HEAD` equals
that SHA before running the authority. A check name or artifact containing the
PR head is not sufficient if the underlying checkout used the synthetic merge
commit.

The acquisition slice has a dedicated
`.github/workflows/acquisition-slice-review.yml` gate. It binds base/candidate
identity first, then independently runs changed-scope Ruff, changed-scope
Pyrefly, full-project Pyrefly, and the focused acquisition/authority/runtime
pytest set. General merge-ref CI remains useful mergeability evidence, but it
must not be substituted for this exact-head authority during #262 review.

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
exact PR head and verify that Pyrefly is both required and successful. Do not
infer this requirement from workflow YAML, a green check list, or draft PR
state. Missing ruleset/branch-protection visibility is incomplete policy
evidence, not proof that no requirement exists.

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

## Issue #269 post-finalizer maintenance

PR #292 completed deterministic finalization in
`b40bc26a5cd14fe1fc136edc5df9a93f060cf90f`. The temporary finalizer helpers
self-deleted after verifying the physical moves, ownership rewrites, facade
deletions, fixture topology, and deletion-only baseline pruning. They must not
be restored or rerun.

Remaining work is normal source/test maintenance by a full-capability Codex
implementation agent. Begin from the exact supplied remote branch head and a
clean worktree, preserve published history, and bind all validation to the
resulting exact remediation head. Substantive production, test, documentation,
lint, and typing repairs are allowed when they implement the final architecture
in `references/issue-269-final-cleanup.md`.

The agent may not run `--update-baseline`, regenerate or re-key the baseline,
add broad ignores, change Pyrefly scope/config/version, weaken tests, or restore
a removed compatibility module. Remaining diagnostics must be fixed at their
real nullable or data-shape boundary. Exact-head validation, rather than
finalizer execution, is the remaining acceptance contract.

All reset-authorized PostgreSQL and Qdrant-mutating tests for #269 must run
through `scripts/disposable-test-services`. Persistent personal services are
never validation targets.

The #269 focused set must include at least:

```text
tests/contract/test_issue_269_final_topology.py
tests/contract/test_package_boundary.py
tests/contract/test_pyrefly_gate.py
tests/contract/test_issue_262_acquisition_slice.py
tests/unit/test_issue_267_composition_root.py
tests/unit/test_issue_263_retrieval_projection_slice.py
tests/contract/test_issue_264_assessment_reporting_slice.py
tests/contract/test_index_checkpoint_contract.py
tests/contract/test_asset_promotion_contract.py
```

Then run the corresponding acquisition, assessment/reporting, retrieval,
checkpoint, reconciliation, fsearch, fscrape, orchestration, release and audit
integration authorities. Central must re-read GitHub CI and merge policy on the
final exact SHA before any review-state or draft-state decision.

## Acquisition-slice review handoff

For issue #262 / PR #284, the local agent must run at the exact current PR head,
not the historical head recorded in an earlier review. At minimum, in addition
to the generic sequence above, focused pytest must include:

```text
tests/contract/test_issue_262_acquisition_slice.py
tests/unit/test_issue_262_runtime_review.py
tests/integration/test_acquisition_service.py
tests/integration/test_acquisition_authority.py
tests/integration/test_issue_216_extraction_preflight.py
tests/integration/test_direct_scrape_service.py
tests/integration/test_authoritative_fsearch.py
tests/integration/test_authoritative_fsearch_review.py
tests/integration/test_authoritative_fscrape.py
tests/unit/test_authoritative_fscrape_cli.py
tests/integration/test_postgres_acquisition_repositories.py
tests/contract/test_package_boundary.py
tests/unit/test_orchestrator.py
tests/integration/test_issue_217_ingestion_batch_semantics.py
tests/integration/test_audit_release_gate_matrix.py
```

Outside the explicit #269 finalizer exception, the local agent must not alter
production code, tests, workflow policy, Pyrefly configuration, or baseline
merely to make this sequence pass. A failure is review evidence to return to
Central.

## Handoff evidence

Report separately:

- exact `git rev-parse HEAD` and authoritative base SHA;
- complete changed-file list;
- changed-scope Ruff check;
- changed-scope Ruff format check;
- changed-scope Pyrefly;
- focused pytest, including skip summary;
- full-project Pyrefly;
- relevant broader contract/integration tests;
- any required disposable PostgreSQL/Qdrant/Valkey runtime evidence.

A failure that requires semantic production changes must be returned as
evidence rather than hidden by weakening typing, lint, test, authority, or
release configuration.
