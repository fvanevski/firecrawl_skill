# Phase 5 gate-closure remediation

## Scope

This document records the narrow remediation required after Phase 5 implementation
PR #292 was merged and the independent gate evaluation of issue #266 was run
against authoritative `main`.

The remediation does **not** reopen Phase 5 architecture design. It repairs two
post-finalization integration defects, addresses the automated Codex review finding
on the remediation itself, and closes the validation/documentation gaps needed
before local exact-head validation and a fresh gate evaluation.

## Findings and resolutions

### Blocking — exact-head release evidence cannot import the canonical package

The first gate evaluation bound to
`a4528d9406acbdf199445502eddbe6dfb6f81b56` found that the push CI
`Release evidence (issue #145)` job failed before producing its manifest.
`generate_exact_head_ci_evidence.py` imports
`firecrawl_skill.research_store.release.evidence`, but the job installed only
dependency requirements and supplied `PYTHONPATH=scripts`. After #269 moved
production ownership exclusively under `src/firecrawl_skill`, that environment no
longer exposes the production package.

Resolution:

- the release-evidence job installs the repository package with
  `python -m pip install --no-deps -e .` after dependency installation;
- the legacy `PYTHONPATH=scripts` override is removed from the generator step;
- the release-invariant job explicitly owns
  `tests/contract/test_phase5_gate_remediation.py`, which proves installation
  precedes generator execution and prevents restoration of the legacy path
  workaround.

This preserves the final package boundary. No compatibility package under
`scripts/`, release-gate weakening, skip, or fallback behavior is introduced.

### Codex review — extensionless `fsearch_smart` retained removed compatibility imports

A Codex review posted after the final independent approval of PR #292 left a live
thread on `scripts/fsearch_smart`. The executable is Python but has no `.py`
suffix, so the existing final-topology AST audit did not inspect it. Merged source
still imported migration-only owners removed by #269:

- `firecrawl_skill.research_store.acquisition_authority`;
- `firecrawl_skill.research_store.container`;
- `firecrawl_skill.research_store.orchestration.composition`.

Resolution:

- acquisition preflight imports from
  `firecrawl_skill.research_store.acquisition.authority`;
- run-service and production-resumable-orchestrator construction import from the
  canonical `firecrawl_skill.research_store.composition` root;
- the new contract discovers extensionless Python entrypoints by shebang and
  checks them against the complete forbidden-module inventory already owned by
  the #269 final-topology contract.

This addresses the concrete caller and the audit-class blind spot rather than
restoring deleted facades.

### Codex review on remediation PR #293 — duplicated retired-module literals

The automated Codex review on PR #293 was bound to stale remediation head
`fe0273d926a8fea3762f55d9a1dd59a33e901e30`. Its thread pointed to the
first version of `tests/contract/test_phase5_gate_remediation.py`, where the test
redeclared retired module names as string literals.

The repository's existing #269 dynamic-target contract correctly rejected those
literals. Central remediation therefore removed the duplicate inventory entirely.
The current test reads `FORBIDDEN_MODULES` from
`tests/contract/test_issue_269_final_topology.py` by AST and applies exact-module
or descendant matching. This also avoids the later false-positive class where a
substring check could confuse canonical `research_store.budget_policy` with the
retired top-level module `budget_policy`.

The original Codex thread is outdated on current source. Repository policy does
not require conversation resolution for merge, and the available GitHub connector
has no thread-resolution mutation; therefore the authoritative disposition is the
current source plus passing exact-head release-invariant contracts, not the stale
thread state.

## Important non-blocking closure requirements

The following are not reasons to broaden the remediation patch, but they are
mandatory before #266 can close:

1. **Merged-head local evidence must be fresh.** PR #292 local results were bound
   to its PR head, not to the later squash-merge SHA. After this remediation PR
   merges, all pre-merge gate evidence is stale.
2. **Phase 1 structural comparison must be explicit.**
   `tools/phase5_architecture_metrics.py` compares the immutable
   `references/architecture-baseline.json` with the final canonical
   `src/firecrawl_skill` package plus remaining Python operator/tooling sources.
   The output is review evidence only; it does not impose LOC or module-count
   gates.
3. **Exact-head CI families remain cumulative.** Ruff, Pyrefly, pytest/contract/
   integration authorities, release invariants, and exact-head release evidence
   must all succeed for the same final `main` SHA.
4. **No validation authority may be weakened.** Do not change the Pyrefly
   baseline/configuration, broaden suppressions, weaken assertions, add skips, or
   bypass the release-evidence manifest to make the remediation green.
5. **PR CI is not push authority.** The repaired `Release evidence (issue #145)`
   job is push/workflow-dispatch only. PR-head success cannot substitute for a
   fresh exact-`main` manifest/artifact after merge.

## Test and documentation coverage added

`tests/contract/test_phase5_gate_remediation.py` provides closure contracts for:

- extensionless Python entrypoints participating in the compatibility reference
  audit;
- `fsearch_smart` importing only final Phase 5 owners;
- release-evidence CI installing the canonical package before generating evidence
  and not using `PYTHONPATH=scripts`;
- the remediation contract itself being explicitly included in the
  release-invariant CI family;
- deterministic Phase 1→Phase 5 structural comparison with explicit evidence-only
  semantics.

`tools/phase5_architecture_metrics.py` reports module count, physical LOC,
top-level-symbol count, size bands, largest modules, and current-minus-baseline
deltas. It uses the same Phase-1 physical-LOC definition,
`len(file_text.splitlines())`, records the scope transition from `scripts/` to the
canonical package plus operator tooling, and imposes no architectural threshold.

## Local Codex CLI handoff contract

The local executor for this gate-validation pass is **Codex CLI using foundation
model `gpt-5.6-sol` at high reasoning effort**. The local agent supplies
host-dependent execution evidence only; Central ChatGPT retains architecture,
substantive remediation, test design, GitHub writes, and the final gate decision.

Use the following authority sequence on the exact remediation PR head and again on
the eventual merged `main` SHA when the gate is restarted:

1. **Native Git authority first.** Run `git fetch origin`, verify a clean working
   tree, check out the exact SHA supplied by Central, and record raw
   `git rev-parse HEAD`. Do not infer identity from a branch name.
2. **Serena (`no-memories`) for semantic inspection.** Inspect
   `scripts/fsearch_smart`, the canonical composition root, acquisition authority
   owner, release-evidence workflow, the #269 final-topology contract, and
   references to removed compatibility surfaces. Confirm no supported caller
   still targets deleted owners. Serena is for symbols/references/structural
   inspection, not generic shell execution.
3. **Ruff on complete changed Python scope.** Run changed-scope lint and format
   checks. Because `scripts/fsearch_smart` is extensionless Python and is not
   selected by `*.py` discovery, also run `ruff check scripts/fsearch_smart`
   explicitly and preserve the raw result.
4. **Repository-pinned Pyrefly.** Run Pyrefly on the complete changed `.py` scope,
   including tests/tools, followed by full-project `pyrefly check`. Also attempt
   `pyrefly check scripts/fsearch_smart` explicitly. If the pinned checker does not
   accept an extensionless Python source path, return that limitation verbatim;
   do not alter Pyrefly configuration, scope, version, baseline, or suppressions
   to manufacture coverage.
5. **Focused regression authority.** Run
   `pytest -q -p no:cacheprovider tests/contract/test_phase5_gate_remediation.py tests/contract/test_exact_head_ci_evidence.py tests/contract/test_issue_269_final_topology.py`.
6. **Structural evidence.** Run
   `python tools/phase5_architecture_metrics.py --source-sha <EXACT_SHA> --output /tmp/phase5-architecture-comparison.json`
   and return the generated metrics plus exact SHA. Treat metrics as review
   evidence, not pass/fail thresholds.
7. **Disposable service authority only.** For PostgreSQL/Qdrant mutation-capable
   gate tests, read `references/local-disposable-test-services.md` at the exact
   SHA and use only `scripts/disposable-test-services`. Exercise `reset-qdrant`
   when projection state can affect results and always run `down`; report raw
   cleanup evidence showing zero helper-owned containers remain.
8. **Broader applicable repository authorities.** Run the relevant unit,
   contract, integration, and release-gate tests necessary to reproduce the
   repository's cumulative gate evidence. Do not redesign production behavior in
   response to a host failure.
9. **RTK is an efficiency layer, not authority.** Use it for routine verbose
   successful output when filtering preserves evidence. Use raw/native output for
   exact SHAs, failures, decisive diffs, migrations, database/service state,
   security/release evidence, and cleanup evidence.
10. **OpenViking is historical context only.** It may provide bounded prior
    rationale with no memories written for this run. It is never authority for
    mutable source, Git/GitHub, CI, PostgreSQL, Qdrant, Valkey, or release state.
11. **No substantive local refactor.** Codex CLI may make narrowly mechanical Ruff
    format/lint repairs only when unambiguous and when they do not change behavior.
    Any semantic defect, test-design defect, architecture question, validation
    failure, or required production change must be returned to Central with raw
    evidence before modification.

Any failure is diagnostic evidence to return to Central. It is not authorization
to weaken tests, assertions, validation, provenance, skip policy, type/lint gates,
release gates, package boundaries, or data-authority checks.
