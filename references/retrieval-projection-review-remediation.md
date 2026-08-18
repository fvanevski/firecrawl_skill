# Issue #263 / PR #285 review remediation

This document records the Central remediation state for PR #285 after the
independent review bound to base
`9ce02b7b555a1ebc4abd46b186b197cc4a8be38f` and reviewed head
`9218f943df2d38ae0c49e671b154fa94f780f5bd`.

The formal independent review at that head is review `4963811089`, state
`CHANGES_REQUESTED`. The remediation commit that edits this document creates a
new PR head; therefore all prior exact-head CI, local validation, and review
conclusions become historical evidence until re-established at the new exact
40-character head.

## Remediation scope

This pass does not redesign retrieval behavior, PostgreSQL authority, Qdrant
projection semantics, index lifecycle rules, lease/checkpoint behavior, or
Pyrefly policy. Central changes only defects or documentation that are properly
owned by #263. Destructive compatibility cleanup and the final physical locality
migration remain owned by #269.

## Blocking review findings

### Fresh local exact-head validation

The independent review could not establish the required local OpenCode evidence
for `9218f943df2d38ae0c49e671b154fa94f780f5bd`. This is an evidence blocker, not a
production-code defect.

**Central disposition:** no source workaround is permitted. This remediation
makes the handoff contract explicit; the local agent must validate the *new*
post-remediation PR head. Previous local PASS results are stale after any Central
commit.

The local agent must report separately:

1. native `git fetch origin`;
2. exact 40-character `git rev-parse HEAD` matching the PR head;
3. the base SHA used and complete ACMR changed-file list;
4. changed-scope `ruff check`;
5. changed-scope `ruff format --check --diff`;
6. changed-scope `pyrefly check <changed.py ...>`, including changed tests;
7. focused #263 pytest plus relevant PostgreSQL/Qdrant/checkpoint/protection
   tests;
8. full-project `pyrefly check`;
9. relevant broader contract/integration suites;
10. base-to-head/worktree `git diff --check`, final exact HEAD, and clean
    worktree evidence.

A local failure is evidence. It does not authorize production-code, test,
Pyrefly-config, baseline, or validation-gate changes.

### Merge-policy evidence is incomplete

At the reviewed head, `gh_get_merge_requirements` returned exact-head,
review/thread, and base-freshness evidence, but reported
`policy_evidence_complete=false` and `checks_evidence_complete=false` because
classic branch-protection visibility returned HTTP 404.

**Central disposition:** this is a Central GitHub-policy/tooling evidence gate,
not a repository implementation defect and not local-agent remediation work. No
PR source change can legitimately turn incomplete GitHub policy visibility into
complete evidence. The local agent must not modify branch protection, CI policy,
tests, required-check configuration, or validation scope to manufacture a green
result.

After fresh local evidence is collected, Central must re-run
`gh_get_merge_requirements` on the then-current exact head. If policy/check-policy
visibility is still incomplete, the PR remains blocked and the GitHub/MCP policy
visibility problem must be remediated separately before approval or merge
readiness can be asserted.

## Important non-blocking findings

### Shadowed retrieval migration files

`scripts/research_store/retrieval.py` and
`scripts/research_store/retrieval_core.py` are non-authoritative migration
residue after `research_store.retrieval` became a package. They contain no domain
classes/functions and must not regain implementation logic.

**Disposition:** unchanged in #263. Issue #269 explicitly owns their deletion
after current-source reference analysis proves supported callers no longer depend
on the migration surfaces. Deleting them now would bypass the compatibility
sequencing required by #269.

### Staged indexing/checkpoint locality

`research_store.retrieval.projection.indexing`,
`checkpoint_indexing_stage`, and the `index_checkpoint_*` facade family expose
the canonical projection namespace while implementation bodies remain at
baseline-stable historical paths.

**Disposition:** unchanged in #263. Issue #269 owns the physical move, caller
audit, facade retirement, and Pyrefly type-debt migration. That work may not
expand `pyrefly-baseline.json`, add broad suppressions, weaken Pyrefly scope or
configuration, or upgrade Pyrefly merely to make the move green. Obsolete
path-keyed baseline entries must be removed after successful relocation.

These two items are therefore tracked architecture debt, not unresolved #263
correctness defects.

## Test/documentation gaps

### Ownership matcher accepted arbitrary prefixes

The reviewed implementation of
`scripts/test_issue_263_retrieval_projection_slice.py::_is_research_store_module`
accepted any module ending in `.research_store.<suffix>`, which meant an
unsupported identity such as
`shadow.research_store.retrieval.ranking` could incorrectly satisfy the structural
ownership oracle.

**Disposition: fixed in this remediation.** The matcher now accepts exactly the
two supported roots:

- `research_store.<suffix>`;
- `firecrawl_skill.research_store.<suffix>`.

The regression now explicitly rejects an arbitrary `shadow.research_store.*`
prefix. This closes the independent review's test gap without changing production
imports or runtime semantics.

### Complete checkpoint-facade wheel coverage

The earlier isolated-wheel gap remains closed: the package-boundary regression
requires and imports the complete checkpoint facade family and verifies identity
with the single staged implementation family. No additional change is required
in this pass.

## Codex Review automated suggestions

Immediately before this remediation, authoritative GitHub evidence for PR #285
showed:

- two formal reviews total (the historical Central COMMENTED review and the
  current Central `CHANGES_REQUESTED` review);
- zero review threads;
- zero PR conversation comments; and
- 35 untruncated checks, none representing a Codex Review suggestion surface.

No Codex Review review, inline thread, conversation comment, check description,
or concrete automated suggestion text was observable. Therefore there is no
Codex implementation item to invent or pre-resolve.

This status is head-sensitive. Central must re-read review/thread/comment/check
state after the remediation head is created and again before final review
closure. Any later concrete Codex suggestion must be dispositioned on its merits.

## Local agent authority boundary

The local OpenCode agent is an execution and evidence collector only.

- **Serena (`no-memories`)**: first-line semantic navigation, changed-symbol and
  reference inspection, dependency implications, and diagnostics. Inspect before
  any mechanical edit and audit afterward.
- **RTK**: compress routine successful Ruff/Pyrefly/pytest/search output only when
  decisive evidence is preserved.
- **OpenViking**: bounded historical rationale only; never authority for mutable
  source, Git, CI, database, runtime, or release state.
- **Native tools**: authoritative exact Git SHAs/diffs, failures, PostgreSQL and
  Qdrant evidence, container/service diagnostics, and final worktree state.

The local agent must not redesign or substantially refactor implementation during
review validation. If validation exposes a substantive defect, report the raw
failure and return remediation to Central unless separately authorized.

## Central post-handoff closure

After the local evidence handoff, Central must:

1. re-fetch PR #285 and bind to the exact current base/head;
2. invalidate the handoff if the head moved;
3. inspect the new base-to-head diff and completeness metadata;
4. re-check current Codex/review/thread/conversation evidence;
5. require successful exact-head Ruff, repository-pinned Pyrefly, focused pytest,
   and applicable broader contract/integration authorities;
6. re-run `gh_get_merge_requirements` and require complete policy/check/review/
   thread/base-freshness evidence before asserting merge readiness; and
7. only then choose a formal review disposition through the dedicated 0.9.0
   review surface.

No merge, ready-for-review transition, approval, issue closure, compatibility
cleanup, or branch-policy weakening is implied by this remediation.
