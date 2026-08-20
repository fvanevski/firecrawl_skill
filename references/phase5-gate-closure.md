# Phase 5 gate-closure remediation

## Scope

This document records the narrow remediation required after Phase 5 implementation
PR #292 was merged and the independent gate evaluation of issue #266 was run
against authoritative `main`.

The remediation does **not** reopen Phase 5 architecture design. It repairs two
post-finalization integration defects and closes the validation/documentation gaps
needed before the local exact-main handoff and a fresh gate evaluation.

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
suffix, so the existing final-topology AST audit did not inspect it. Current source
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
  rejects imports of the removed compatibility surfaces.

This addresses the concrete caller rather than restoring deleted facades.

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

## Test and documentation coverage added

`tests/contract/test_phase5_gate_remediation.py` provides four closure contracts:

- extensionless Python entrypoints participate in the compatibility reference
  audit;
- `fsearch_smart` imports only final Phase 5 owners;
- release-evidence CI installs the canonical package before generating evidence
  and does not use `PYTHONPATH=scripts`;
- the remediation contract itself is explicitly included in the release-invariant
  CI family.

It also exercises the Phase 5 structural-comparison tool twice and requires
byte-identical output for the same source SHA.

## Local OpenCode handoff after the remediation PR head is fixed

The local agent supplies execution evidence only; Central ChatGPT retains the gate
decision.

Use the following authority sequence on the exact remediation PR head and again on
the eventual merged `main` SHA when the gate is restarted:

1. Native Git: `git fetch origin`, clean-tree verification, checkout the exact
   supplied SHA, and raw `git rev-parse HEAD`.
2. Serena with `no-memories`: inspect `scripts/fsearch_smart`, the composition
   root, the acquisition authority owner, the release-evidence workflow, and
   references for removed compatibility surfaces. Confirm no supported caller
   still targets the deleted owners.
3. Ruff on the complete changed `.py` scope, followed by Ruff format checking.
   Because `scripts/fsearch_smart` is extensionless Python and is not selected by
   `*.py` discovery, also run `ruff check scripts/fsearch_smart` explicitly and
   report its raw result.
4. Repository-pinned Pyrefly on the complete changed `.py` scope, including tests
   and tools, followed by full-project `pyrefly check`. Also attempt
   `pyrefly check scripts/fsearch_smart` explicitly. If the pinned checker does not
   accept an extensionless Python source path, report that limitation verbatim;
   do not alter Pyrefly configuration, scope, version, baseline, or suppressions
   to manufacture coverage.
5. Focused contracts:
   `pytest -q -p no:cacheprovider tests/contract/test_phase5_gate_remediation.py
   tests/contract/test_exact_head_ci_evidence.py
   tests/contract/test_issue_269_final_topology.py`.
6. Generate structural evidence:
   `python tools/phase5_architecture_metrics.py --source-sha <EXACT_SHA>
   --output /tmp/phase5-architecture-comparison.json`.
7. For PostgreSQL/Qdrant mutation-capable gate tests, read
   `references/local-disposable-test-services.md` at that SHA and use only
   `scripts/disposable-test-services`. Exercise `reset-qdrant` when projection
   state can affect results and always `down` with zero helper-owned containers
   remaining.
8. Run the broader applicable repository test/integration authorities.
9. RTK may compress routine successful output. Use raw/native output for exact
   SHAs, failures, decisive diffs, database/service state, release evidence, and
   cleanup evidence.
10. OpenViking may supply bounded historical rationale only; it is not authority
    for source, Git, CI, PostgreSQL, Qdrant, Valkey, or release state.

Any failure is evidence to return to Central. It is not authorization for local
architectural redesign or validation-policy changes.
