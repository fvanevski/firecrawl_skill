# Phase 5 gate-closure remediation

## Scope

This document records the narrow remediation required after Phase 5 implementation
PR #292 was merged and the independent gate evaluation of issue #266 was run
against authoritative `main`.

The remediation does **not** reopen Phase 5 architecture design. It repairs
post-finalization integration defects, addresses automated Codex review findings,
and closes validation/documentation gaps needed before local exact-head validation
and a fresh gate evaluation.

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

### Blocking local validation at `74f6c2b7465e82108392384e62114949596bc829`

The first Codex CLI host validation of PR #293 was **not** a clean pass. The local
agent correctly exercised `scripts/fsearch_smart` explicitly instead of relying on
normal `*.py` discovery and exposed latent static-validation failures that predated
PR #293 but were newly in scope for Phase 5 closure:

- Ruff reported three `I001` import-order findings in the extensionless executable;
- `ruff format --check` reported that the executable required formatting;
- repository-pinned Pyrefly `1.1.1` accepted the extensionless path and reported a
  type error where an integer `max_adaptive_cycles` value was written through a
  nested dictionary inferred as string-valued.

Git history and the PR diff showed that the failing locations were not introduced
by PR #293. That fact did **not** make them non-blocking: once the Phase 5 gate
requires explicit static authority over a production extensionless Python
entrypoint, pre-existing failures are gate failures.

Central resolution:

- `scripts/fsearch_smart` import sections are Ruff-normalized, including the
  `scripts/`-resolved import group and canonical `firecrawl_skill` group;
- long in-function imports are formatted consistently;
- `dry_run(...)` now declares `-> dict[str, Any]`, matching its intentionally
  heterogeneous JSON-like payload and eliminating the false narrow nested-dict
  inference without changing runtime behavior;
- the Ruff CI job now runs ordinary Ruff lint, explicit import-order lint, and
  format checking against `scripts/fsearch_smart` directly;
- the Pyrefly CI job now runs pinned Pyrefly directly against
  `scripts/fsearch_smart` in addition to full-project and changed-`.py` scope;
- `tests/contract/test_phase5_gate_remediation.py` asserts that those direct
  extensionless Ruff/Pyrefly authorities remain CI-owned.

The first central correction was itself rejected by the new Ruff CI step because
its top import block still mixed Ruff's `scripts/`-resolved and canonical-package
sections. Central corrected the section boundary rather than changing Ruff policy.
At remediation head `2dbdb207dcbbd2fe62b0cfa932673d442d3b027e`, the complete
PR-event CI workflow passed, including explicit extensionless Ruff and Pyrefly, and
ARC-17 also passed. Any later documentation-only head must be rechecked before
handoff.

### Superseded but informative local evidence from `74f6c2b...`

The failed local pass also produced strong evidence that the static failures were
narrow rather than symptoms of wider semantic breakage:

- clean Git tree, exact expected head/remote identity, and correct merge-base;
- Serena `no-memories` confirmed the canonical acquisition/composition owners and
  found no supported caller of the deleted compatibility owners;
- changed-`.py` Ruff/format and repository-wide normal-discovery Ruff/format passed;
- changed-`.py` Pyrefly and full-project Pyrefly passed with zero errors;
- focused remediation regressions: 28 passed;
- release-invariant family: 110 passed;
- broad CI family: 1,789 passed with 2 expected skips;
- strict campaign plus preflight: 59 passed with 2 expected skips;
- strict integration class: 8 passed with 2 expected skips;
- credential-free skip allowlist verification passed with all skips classified and
  no stale or unknown entries;
- canonical `pip install --no-deps -e .` reproduced successfully, release-evidence
  imports resolved from `src/firecrawl_skill`, and the evidence generator started;
- disposable validation namespaces were shut down and ports `55436`/`55437` were
  closed; unrelated pre-existing exited `fc269_pg` and `fc269_qdrant` containers
  were left untouched.

The first broad skip-classifier attempt was invalid for skip-classification evidence
because the host had `OPENAI_API_KEY` set, causing an allowlisted LLM test to run
and pass. Re-running with credentials removed produced the expected authoritative
classification. Local gate instructions therefore require credential-free
skip-classifier execution when the expected skip inventory depends on absent
credentials.

All evidence in this subsection is **superseded for merge/gate purposes** because
the remediation branch advanced after it was collected. It may be used only to
explain defect isolation and to define the required rerun.

## Important non-blocking closure requirements

The following are not reasons to broaden the remediation patch, but they are
mandatory before #266 can close:

1. **Merged-head local evidence must be fresh.** Any local results bound to an
   earlier PR head become stale immediately when the branch advances. After this
   remediation PR merges, all PR-head evidence is stale again.
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
6. **Extensionless Python is explicit static scope.** A green repository-wide
   Ruff/Pyrefly pass that omits `scripts/fsearch_smart` is insufficient. Both CI
   and local validation must exercise it directly.

## Test and documentation coverage added

`tests/contract/test_phase5_gate_remediation.py` provides closure contracts for:

- extensionless Python entrypoints participating in the compatibility reference
  audit;
- `fsearch_smart` importing only final Phase 5 owners;
- direct Ruff lint/import-order/format and pinned Pyrefly CI ownership of the
  extensionless production executable;
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

The local artifact generated at the failed `74f6c2b...` pass reported:

| Metric | Phase 1 | Phase 5 | Delta |
| --- | ---: | ---: | ---: |
| Modules | 195 | 269 | +74 |
| Physical LOC | 93,441 | 90,389 | -3,052 |
| Top-level symbols | 1,222 | 1,331 | +109 |

Those values remain evidence-only and are stale for exact-head identity after the
remediation branch advanced. A fresh artifact must be generated on the handoff SHA.

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
3. **Ruff on complete changed Python scope plus the extensionless executable.**
   Run changed-`.py` lint and format checks and repository-wide normal-discovery
   checks. Then run all of the following explicitly:

   ```bash
   ruff check scripts/fsearch_smart
   ruff check --select I scripts/fsearch_smart
   ruff format --check --diff scripts/fsearch_smart
   ```

   Any non-zero result is blocking.
4. **Repository-pinned Pyrefly `1.1.1`.** Run Pyrefly on the complete changed
   `.py` scope, including tests/tools, followed by full-project `pyrefly check`.
   Then run `pyrefly check scripts/fsearch_smart` explicitly. Pyrefly `1.1.1` is
   known to accept this extensionless path; any diagnostic is blocking. Do not
   alter configuration, scope, version, baseline, or suppressions to manufacture
   a pass.
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
   cleanup evidence showing zero helper-owned containers remain. Do not remove or
   repurpose unrelated pre-existing containers.
8. **Broader applicable repository authorities.** Run the relevant unit,
   contract, integration, release-gate, and skip-classification tests necessary to
   reproduce cumulative gate evidence. When verifying a skip inventory whose
   contract assumes absent external credentials, remove `OPENAI_API_KEY` and other
   applicable credentials from that test environment first and report the
   credential-free result.
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
