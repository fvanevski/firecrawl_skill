# Issue #263 / PR #285 review remediation

This document records Central remediation for PR #285. It is a scope,
finding-disposition, and validation contract; it is not a substitute for fresh
exact-head GitHub or local runtime evidence.

## Review revisions

The first local validation findings were produced at:

- base: `9ce02b7b555a1ebc4abd46b186b197cc4a8be38f`;
- reviewed head: `ab9c83545dfc7c0cbd8b26f3f354f2f92dfec57f`.

The later independent Central review was bound to:

- base: `9ce02b7b555a1ebc4abd46b186b197cc4a8be38f`;
- reviewed head: `556e8230b944e4b049b5cc0410a74861cac4e63d`;
- formal review ID: `4963113015`;
- formal review state: `COMMENTED`.

The commit containing the remediation in this document necessarily creates a new
PR head. All CI, local-test, and review conclusions tied to either earlier head
are historical evidence only until repeated or independently re-established at
the new exact 40-character head.

## Independent-review finding disposition

### Blocking findings

The independent review found **no blocking implementation finding** at
`556e8230b944e4b049b5cc0410a74861cac4e63d`. There is therefore no production
correctness repair to invent in this remediation.

The earlier source-test import-prefix blocker was already corrected by making the
#263 structural ownership checks exact about the `research_store.<suffix>`
boundary while accepting the two supported package roots.

### Important non-blocking: shadowed retrieval migration files

Affected files:

- `scripts/research_store/retrieval.py`
- `scripts/research_store/retrieval_core.py`

After `research_store.retrieval` became a package, these sibling files are not
the authoritative ordinary import boundary. They contain no classes or functions
and must not acquire domain logic.

**Disposition:** intentionally retained for the migration phase and carried as a
mandatory deletion item in #269. This is not an untracked deferral. Issue #269
explicitly owns removal of migration-only compatibility scaffolding only after
current-source reference analysis proves supported callers have migrated.

#263 now makes the temporary state testable:
`scripts/test_issue_263_retrieval_projection_slice.py` fails if either file
regains a class or function definition, and
`scripts/test_issue_263_package_boundary.py` proves that installed canonical
imports resolve to the `retrieval/` package.

The smallest correct final remediation is destructive deletion under #269 after
its caller/reference audit. Deleting them opportunistically in #263 would violate
the campaign's sequencing/compatibility authority.

### Important non-blocking: staged indexing/checkpoint locality

Affected canonical namespace:

- `research_store.retrieval.projection.indexing`
- `research_store.retrieval.projection.checkpoint_indexing_stage`
- `research_store.retrieval.projection.index_checkpoint_*`

The projection namespace currently exposes zero-domain-logic facades over a
single historical root implementation family.

**Disposition:** the canonical ownership boundary is accepted for #263; physical
implementation relocation is mandatory #269 work, coupled to a reviewed Pyrefly
type-debt migration. The existing baseline contains path-keyed debt in the
historical indexing/checkpoint files. #263 must not hide that debt by expanding
`pyrefly-baseline.json`, adding broad suppressions, or weakening type-check scope.

The temporary arrangement is now independently constrained in two ways:

1. the source structural regression requires every checkpoint projection facade
   to define no class/function implementation; and
2. the isolated-wheel regression imports the complete facade family and verifies
   identity with the single historical implementation.

#269 must physically relocate the implementation, resolve/review the affected type
debt, remove obsolete baseline entries, and then retire the temporary facades.

### Test/documentation gap: incomplete checkpoint wheel coverage

The prior isolated-wheel regression omitted explicit member/import checks for:

- `index_checkpoint_asset_membership.py`
- `index_checkpoint_core.py`
- `index_checkpoint_finalize.py`
- `index_checkpoint_models.py`
- `index_checkpoint_replay.py`
- `index_checkpoint_store.py`

**Disposition: fixed in #263.**

`scripts/test_issue_263_package_boundary.py` now enumerates every new checkpoint
projection facade as a required wheel member, imports each facade outside the
repository source tree, imports the corresponding historical implementation, and
proves object identity across the staged boundary.

The source structural regression independently checks the complete facade family
for zero domain definitions.

### Codex Review automated suggestions

At the last reviewed exact head, authoritative GitHub PR review/thread state and
PR conversation metadata exposed no Codex Review review, thread, or automated
suggestion text. No concrete Codex suggestion is therefore available to
implement.

**Disposition:** none outstanding in observable GitHub state. This remediation
does not fabricate or infer unseen bot comments. If Codex posts a concrete
suggestion at a later head, it becomes new exact-head evidence and must be
reviewed on its merits before phase-gate closure.

## Previous remediation retained

The earlier remediation remains in force:

- source tests and canonical source/wheel imports may legitimately use different
  leading module prefixes;
- the dedicated exact-head workflow executes the issue-specific structural and
  package regressions;
- nested retrieval/projection setuptools mappings are tested in an isolated
  built wheel;
- PostgreSQL/Qdrant authority and compatibility surfaces are documented in
  `references/retrieval-projection-boundary.md`;
- `scripts/conftest.py` path ordering is intentionally unchanged;
- the unrelated
  `test_candidate_replay_api.py::test_list_candidates_paginated_invalid_parameters`
  failure remains out of #263 because it reproduces at the immutable base; and
- informational Pyrefly `unnecessary-type-conversion` warnings in moved Qdrant
  code are not converted into runtime changes merely to silence warnings.

## Exact-head remote gate

`.github/workflows/retrieval-projection-slice-review.yml` performs:

1. immutable candidate checkout and SHA assertion;
2. exact base-to-head `git diff --check`;
3. ACMR changed-Python derivation;
4. changed-scope Ruff lint and formatter checks;
5. changed-scope Pyrefly including changed tests;
6. full-project Pyrefly;
7. disposable PostgreSQL and Qdrant startup;
8. focused #263 structural/package/retrieval/projection/reconciliation/checkpoint
   tests; and
9. bounded JSON evidence recording base, candidate, tested SHA, and authority
   families.

The gate must not update the Pyrefly baseline, add suppressions, change Pyrefly
version/scope, or mutate non-disposable infrastructure.

## Fresh local handoff requirement

The Central remediation commit invalidates every previous local exact-head PASS.
Local OpenCode validation must begin by fetching and verifying the new
40-character PR head.

The local agent is an execution/evidence layer:

- **Serena (`no-memories`)**: first-line semantic navigation, changed symbols,
  references, dependencies, and diagnostics; inspect before any mechanical edit
  and audit afterward.
- **RTK**: routine successful Ruff/Pyrefly/pytest/search output when compression
  preserves decisive evidence.
- **OpenViking**: bounded historical rationale only; never authority for current
  source, Git, CI, database, or runtime state.
- **Native tools**: exact Git SHAs/diffs, failures, PostgreSQL/Qdrant runtime
  evidence, service/container diagnostics, and final worktree state.

Minimum local validation at the new exact head:

1. `git fetch origin`, exact 40-character HEAD verification, base SHA, and
   complete ACMR changed-file list;
2. changed-scope `ruff check`;
3. changed-scope `ruff format --check --diff`;
4. changed-scope `pyrefly check <changed.py ...>`, including changed tests;
5. #263 structural/package/corpus-service regressions;
6. focused retrieval/projection/PostgreSQL/Qdrant/checkpoint/protection tests;
7. full-project `pyrefly check`;
8. relevant broader integration/contract tests;
9. base-to-head and worktree `git diff --check`; and
10. final exact HEAD plus clean worktree evidence.

A validation failure is evidence, not permission to alter production code,
tests, Pyrefly configuration, baseline, or authority gates. Substantive
remediation returns to Central unless separately authorized.

## Central source-remediation closure

Before local handoff, Central source remediation is complete when:

- the full checkpoint facade wheel/import gap is closed;
- temporary compatibility/locality exceptions are explicit, tested, and carried
  into #269's authoritative cleanup scope;
- current observable Codex Review state has no undispositioned suggestion;
- the PR body no longer represents stale local evidence as current; and
- fresh exact-head remote Ruff, Pyrefly, package, pytest/contract/integration
  authorities succeed on the new head.

No merge, ready-for-review transition, approval, issue closure, or #269 cleanup
is implied by this document.
