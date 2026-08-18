# Issue #263 review remediation

This document records the Central remediation applied to PR #285 after the first
local exact-head validation of the retrieval/projection vertical slice. It is a
scope and validation contract, not a substitute for fresh GitHub or local
runtime evidence.

## Reviewed revision

The local review that produced the findings was bound to:

- base: `9ce02b7b555a1ebc4abd46b186b197cc4a8be38f`;
- reviewed head: `ab9c83545dfc7c0cbd8b26f3f354f2f92dfec57f`.

Any remediation commit creates a new exact head. Earlier CI/local evidence is
then historical diagnostic evidence only and must not be represented as final
validation of the new revision.

## Finding-to-remediation map

| Finding | Disposition | Remediation |
|---|---|---|
| Blocking: seven #263 structural assertions rejected modules loaded as `research_store.*` by the repository's own pytest bootstrap | Fixed in test contract | `scripts/test_issue_263_retrieval_projection_slice.py` now matches the exact capability-relative `research_store.<suffix>` boundary while accepting either the historical source-test root or canonical `firecrawl_skill.` prefix. The matcher has negative coverage so unrelated suffix lookalikes are not accepted. |
| Important non-blocking: source tests and canonical source/wheel imports legitimately produce different leading module prefixes | Documented and independently tested | `references/retrieval-projection-boundary.md` records both supported contexts. `scripts/test_issue_263_package_boundary.py` builds the wheel, checks retrieval/projection contents, imports it outside the repository path, and proves canonical/legacy same-object identity. Global `scripts/conftest.py` path ordering is intentionally unchanged. |
| Test gap: the new #263 structural suite was not present in the broad CI test list | Closed with stronger authority | `.github/workflows/retrieval-projection-slice-review.yml` executes the #263 structural and package regressions on the immutable candidate SHA together with the directly relevant retrieval/projection contracts. Broad merge-ref CI remains supplementary. |
| Test gap: nested retrieval/projection setuptools mappings were not independently proven in an isolated built wheel | Fixed | `scripts/test_issue_263_package_boundary.py` verifies required wheel members and canonical module identities with repository paths excluded. |
| Documentary gap: no durable map explained PostgreSQL/Qdrant authority, compatibility aliases, dual source import contexts, or the baseline-stable checkpoint/indexing facade decision | Fixed | `references/retrieval-projection-boundary.md` records the authority map, import contract, compatibility surfaces, baseline rationale, and test authority. |
| Documentary gap: the local validation failure and its remediation were only present in chat evidence | Fixed | This document records the exact reviewed revision, failure classification, remediation, and fresh-validation requirement. |
| Non-blocking local failure: `test_candidate_replay_api.py::test_list_candidates_paginated_invalid_parameters` fails at the immutable base too | Out of scope / pre-existing | No #263 code or tests are changed to hide it. It remains separate debt because the failing call targets the UoW rather than the acquisition repository and is demonstrably present at the issue base. |
| Non-blocking Pyrefly warnings in moved Qdrant code (`unnecessary-type-conversion`) | No correctness/type-gate defect | Pyrefly reports zero errors and the conversions are inherited defensive normalization in the physically moved Qdrant implementation. #263 does not change runtime normalization merely to silence informational warnings. A later type-hygiene cleanup may remove them with its own behavioral review. |

## Automated-review disposition

At the reviewed exact head, the available GitHub review surfaces expose no
formal reviews, review threads, requested reviewers, or PR conversation comments.
No PR-specific Codex Review suggestion text is therefore available through the
authoritative GitHub tooling, and this remediation does not fabricate or claim
to resolve unseen bot comments.

The patch was nevertheless reviewed for the concrete automated-review classes
that are reproducible from source. The resulting gaps are addressed here:

- import-context-sensitive ownership assertions;
- missing isolated wheel coverage for newly registered nested packages;
- missing exact-head execution of the issue-specific structural tests;
- ambiguity around compatibility alias/module identity;
- undocumented path-sensitive Pyrefly baseline rationale.

If a later Codex Review posts a concrete current-head finding, it must be fetched
and dispositioned against that exact head before #260 gate closure. A new review
finding is not pre-resolved by this document.

## Why `scripts/conftest.py` is not changed

The local failure is caused by an assertion assuming every source test uses the
canonical package prefix, not by a production import failure. The repository's
historical pytest bootstrap intentionally puts `scripts/` first, so changing it
would alter import identity across a large unrelated test corpus. The smallest
correct remediation is to make structural ownership assertions prefix-aware and
separately prove the canonical installed-package identity.

## Exact-head review gate

`.github/workflows/retrieval-projection-slice-review.yml` performs, in order:

1. immutable candidate checkout and SHA assertion;
2. exact base-to-head `git diff --check`;
3. ACMR changed-Python derivation;
4. changed-scope Ruff lint and formatter checks;
5. changed-scope Pyrefly including changed tests;
6. full-project Pyrefly;
7. disposable PostgreSQL and Qdrant startup;
8. focused #263 structural/package/retrieval/projection/reconciliation/checkpoint tests;
9. a bounded JSON artifact recording base, candidate, tested SHA, and authority families.

The gate does not update `pyrefly-baseline.json`, add suppressions, change
Pyrefly version/scope, or mutate non-disposable infrastructure.

## Fresh local handoff requirement

After this remediation commit, local OpenCode validation must be rerun against
the new exact 40-character PR head. The previous `ab9c835...` local report is no
longer final exact-head evidence.

The local agent remains an execution/evidence layer:

- Serena (`no-memories`) for semantic source/reference diagnostics;
- RTK for routine successful Ruff/Pyrefly/pytest output where compression
  preserves the decisive result;
- OpenViking only for bounded historical rationale;
- native Git/raw runtime tooling for exact SHAs, changed-file evidence, failures,
  PostgreSQL/Qdrant runtime evidence, and final diff hygiene.

Minimum local validation must repeat changed-scope Ruff, changed-scope Pyrefly,
the issue-specific structural/package tests, relevant retrieval/projection
regressions, full-project Pyrefly, and exact `git diff --check` at the new head.
No substantive local repair is authorized by this document.

## Closure condition

Source-level remediation is complete only when fresh evidence at one unchanged
current PR head shows:

- the exact-head retrieval/projection review workflow succeeds;
- local OpenCode validation succeeds at that same SHA;
- current required Ruff/Pyrefly and relevant PostgreSQL/Qdrant/checkpoint gates
  are green;
- no current-head review/thread finding remains unresolved;
- package/wheel imports preserve canonical identity and legacy same-object
  compatibility.

No merge, ready-for-review transition, approval, or issue closure is implied by
this remediation alone.
