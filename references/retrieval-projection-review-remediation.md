# Issue #263 / PR #285 review remediation

This document is the repository-side closure record for the independent review
of PR #285. It distinguishes defects that Central can remediate in the branch
from exact-head evidence that must be collected by the local OpenCode agent.

The original independent disposition was bound to base
`9ce02b7b555a1ebc4abd46b186b197cc4a8be38f`. Any Central remediation commit
creates a new PR head, so local evidence and formal-review conclusions from an
older head are historical only. The local agent must always resolve and verify
the current 40-character PR head before validation.

## Scope and authority

Issue #263 is a topology/locality refactor. This remediation does not redesign
retrieval semantics, PostgreSQL durable authority, Qdrant reconstructability,
index lifecycle policy, lease/checkpoint behavior, or Pyrefly debt policy.

Destructive compatibility cleanup and the final physical indexing/checkpoint
relocation remain owned by #269. Performing that work here would violate the
campaign's caller-audit and type-debt sequencing rather than resolve a #263
defect.

## Finding disposition matrix

| Review item | Classification | Repository-side disposition before local handoff |
|---|---|---|
| Fresh independent OpenCode exact-head validation missing | Blocking evidence | No source workaround. Central encodes the exact handoff contract below; local validation remains the only unresolved blocker after this commit. |
| Merge-policy/check-policy visibility | Blocking evidence at an earlier head | Central subsequently obtained complete policy/check/review/thread/base-freshness evidence. Re-check is still mandatory on the post-remediation head. |
| `retrieval.py` / `retrieval_core.py` migration residue | Important non-blocking | Intentionally retained zero-domain-logic scaffolding; #269 owns reference-audited deletion. #263 structural tests prevent implementation from returning to these files. |
| Staged root indexing/checkpoint implementations | Important non-blocking | Intentionally retained until #269 performs the reviewed physical move/type-debt migration. Projection facades must remain zero-domain-logic and identity-equivalent. |
| Ownership matcher accepted arbitrary prefixes | Test gap | Fixed: only `research_store.*` and `firecrawl_skill.research_store.*` are accepted, with a negative shadow-prefix regression. |
| Isolated-wheel checkpoint facade coverage | Test gap | Fixed: the package regression enumerates/imports the complete facade family and verifies object identity. |
| Dedicated slice workflow omitted staged root implementation paths | Test/documentation gap | Fixed in this remediation: workflow paths now include root `indexing.py`, `checkpoint_indexing_stage.py`, and `index_checkpoint_*.py`; a structural regression locks those trigger entries. |
| Codex Review automated suggestions | Conditional | No concrete Codex review, thread, conversation comment, or check suggestion is currently observable through authoritative GitHub surfaces. There is therefore no code item to invent. Central must re-read these surfaces after the new head exists and before final disposition. |

## Blocking local exact-head evidence

The remaining blocker is evidence, not a production-code defect. Central must
not weaken tests, Pyrefly, CI, branch policy, or implementation invariants to
replace the independent local run.

The local OpenCode agent must:

1. use native Git to `git fetch origin`;
2. resolve the current PR head and verify exact `git rev-parse HEAD` against that
   40-character SHA, preferably detached or in an isolated review worktree;
3. record the exact base SHA and the complete ACMR changed-file list;
4. use Serena (`no-memories`) first for changed-symbol, reference, dependency,
   and diagnostic inspection;
5. run `ruff check` on the exact changed Python paths;
6. run `ruff format --check --diff` on those same paths;
7. run repository-pinned `pyrefly check <changed.py ...>` on the complete changed Python set, including changed tests explicitly;
8. run the smallest deterministic #263 structural/package/retrieval/projection/
   PostgreSQL/Qdrant/checkpoint/protection pytest set capable of falsifying the
   change;
9. run full-project `pyrefly check` with no file arguments;
10. run the relevant broader contract/integration authorities;
11. run base-to-head/worktree `git diff --check`; and
12. report the final exact HEAD and clean-worktree status.

RTK may compress routine successful output only when decisive evidence is
preserved. Raw/native output remains required for exact SHAs, complete decisive
diffs, failures, transaction/concurrency/database evidence, and final worktree
state. OpenViking is historical rationale only and is never authority for
mutable source, GitHub, CI, database, runtime, or release state.

A local failure is evidence. It does not authorize production-code, test,
Pyrefly-config, baseline, or validation-gate changes. Substantive remediation
returns to Central unless separately authorized.

## Important non-blocking architecture findings

### Shadowed retrieval migration files

`src/firecrawl_skill/research_store/retrieval.py` and
`src/firecrawl_skill/research_store/retrieval_core.py` are non-authoritative migration
residue after `research_store.retrieval` became a package. They must remain free
of domain classes/functions. No new supported caller may depend on either file.

Issue #269 owns deletion only after current-source reference analysis proves the
remaining supported callers have migrated. Deleting them in #263 would bypass a
required compatibility audit.

### Staged indexing/checkpoint locality

`research_store.retrieval.projection.indexing`,
`checkpoint_indexing_stage`, and the `index_checkpoint_*` projection facade
family expose the canonical projection namespace while implementation bodies
remain at baseline-stable root paths.

Issue #269 owns the physical move, caller audit, facade retirement, and reviewed
Pyrefly type-debt migration. That work may not expand `pyrefly-baseline.json`,
add broad suppressions, weaken Pyrefly scope/configuration, or upgrade Pyrefly
merely to make the move green. Stale path-keyed baseline entries must be removed
only after successful relocation.

While this staging exists, the dedicated retrieval/projection slice workflow now
tracks changes to the actual root implementations as well as changes under the
projection namespace. `test_slice_workflow_tracks_staged_root_implementations`
prevents the trigger coverage from silently regressing.

## Codex Review automated suggestions

Immediately before this remediation, authoritative GitHub evidence showed three
formal reviews total, zero review threads, zero PR conversation comments, and no
Codex-specific review/check suggestion text. The current Central
`CHANGES_REQUESTED` review is evidence about this review contract, not a Codex
Review suggestion.

Accordingly, there is no concrete Codex recommendation to implement at this
point. This is deliberately fail-closed rather than an assumption that Codex has
nothing to say: after the remediation commit creates a new head, Central must
re-read reviews, unresolved threads, conversation comments, and checks. Any
newly surfaced Codex suggestion must be inspected and dispositioned before final
review closure.

## Central post-handoff closure

After the local evidence handoff, Central must:

1. re-fetch PR #285 and bind to the exact current base/head;
2. invalidate local evidence if the head moved;
3. inspect the exact base-to-head diff and all completeness/digest metadata;
4. re-read Codex/review/thread/conversation/check state;
5. require separate successful exact-head Ruff, repository-pinned Pyrefly,
   focused pytest, and applicable broader contract/integration authorities;
6. re-run `gh_get_merge_requirements` and require complete policy/check/review/
   thread/base-freshness evidence; and
7. only then select the appropriate formal 0.9.0 review action.

No merge, ready-for-review transition, approval, issue closure, #269 cleanup, or
branch-policy weakening is implied by this remediation.
