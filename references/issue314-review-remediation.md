# Issue #314 review-remediation authority map

This record maps the central source/documentation remediation for issue #314 before host-bound validation. It is not test evidence: current source at the exact branch SHA, repo-native static checks, disposable-service tests, and exact Git identity remain authoritative.

## Target invariant

Normal research has one public deterministic control plane: `scripts/fresearch`. The controller owns retained-first progression, persisted semantic planning, bounded acquisition, evidence/coverage authority, terminal progression, durable human actions, and final delivery. The outer agent supplies a research objective and genuine human decisions; it does not reconstruct low-level workflow commands or generated internal parameters.

Default `host_handoff` delivery must complete without redundant inner-model full-prose drafting while preserving the same fail-closed evidence, temporal, membership, and terminal-decision authority required of a completed run.

## Blocking findings and resolutions

### Host-handoff completion could not satisfy the terminal gate

**Defect:** `host_handoff` skipped draft/citation generation, but terminal completion still required the self-synthesized semantic artifact chain.

**Resolution:** `completion_provenance.py` now has a distinct `HostHandoffCompletionProvenance`. It verifies exact sealed run membership, the exact current EvidencePacket, persisted claims, persisted claim/evidence links, and deterministic packet/hash census. The terminal answer-hash storage slot contains a deterministic handoff-authority digest; no placeholder synthesis artifact is manufactured. Runs without the issue-314 controller policy retain the established self-synthesized provenance path.

### Public handoff was not bound to the exact completed EvidencePacket

**Defect:** final delivery could load the latest packet and then copy completion revision/hash metadata without proving that the returned packet was the packet certified at terminal completion.

**Resolution:** completed handoff construction hashes the packet actually being returned and requires its revision/hash to equal completion provenance. Coverage is loaded from the packet's exact persisted coverage snapshot. A completed lifecycle whose canonical handoff cannot be reconstructed is exposed as blocked delivery with `result_ready=false`, not as a usable completed result.

### Public status and result could contradict each other

**Defect:** `result()` failed closed on a broken completed handoff, while `status()` could still report `terminal_completed` and `result_ready=true` merely because the lifecycle was terminal.

**Resolution:** completed `status()` now requires a verifiable canonical handoff before reporting ready/completed delivery. Persisted `objective_satisfied` remains a lifecycle fact; delivery readiness is separately fail-closed. A blocked directive attached to a terminal lifecycle is also mapped to non-resumable CLI exit status `1`, rather than the resumable checkpoint status `75` used for nonterminal blockers/actions.

## Important non-blocking and automated-review hardening

- `scripts/fsearch_smart` is a policy-free `exec` delegate to the real `scripts/fresearch` wrapper. It owns no environment, planning, lifecycle, budget, resume, preview, or recovery policy.
- `research-result-v3` structurally references `research-handoff-v1`; the handoff schema now constrains bounded nested claims, passages, bindings, unresolved items, temporal data, objective authority, and delivery hashes.
- Public handoff material replaces claim/passage/coverage identities with bounded synthetic aliases or semantic summaries. ResearchSpec IDs, coverage-item IDs, membership-seal IDs/hashes, EvidencePacket IDs, candidate/snapshot/chunk IDs, and generated recovery parameters are not public normal-agent authority.
- `ControllerPolicy` defaults to `host_handoff`; older controller/specialist runs lacking that policy field retain self-synthesized compatibility rather than being silently reinterpreted.
- `HandoffBuilder` remains an internal read-only assembly helper; the controller performs the public sanitization and terminal packet/provenance binding.
- The orchestrator source now describes itself as the application engine behind the controller/specialist composition roots, not as a second public controller.

## Documentation remediation

Current normative/current-facing documentation is controller-first:

- `SKILL.md`
- `README.md`
- `references/authoritative-workflows.md`
- `references/workflow-state-schema.md`
- `references/operations-runbook.md`
- `references/research-store-operations.md`
- `references/coding-agent-guide.md`
- `references/budget-policy.md`

These documents no longer teach unprepared direct acquisition, manual normal-agent `frun finish` choreography, `fsearch_smart --dry-run`, spec-skeleton/manual ResearchSpec injection, generated candidate-budget check parameters, or exit-75 smart recovery recipes.

Issue/audit/remediation records that preserve superseded command behavior are explicitly historical/non-normative where ambiguity remained, including issue #305, #307, #311, and the earlier orchestration-boundary record.

## Regression mapping

Fast/contract authority includes:

- `tests/contract/test_issue314_handoff_skill_surface.py`: public surface, schema linkage, host-handoff stage behavior, v2 provenance fields/tamper rejection, documentation boundary.
- `tests/unit/test_issue310_research_controller.py`: completed status fails closed when the canonical handoff is not verifiable.
- compatibility and documentation contracts under `tests/contract/test_documented_workflows.py`, `test_issue302_operator_guidance.py`, `test_issue310_fresearch_surface.py`, `test_phase5_gate_remediation.py`, `test_rc10_skill_guidance.py`, and `test_workflow.py`.
- #305/#307/#311 unit suites now target the surviving semantic/query/controller services or the exact compatibility-delegation invariant rather than requiring the retired second controller.

PostgreSQL-backed authority includes:

- `tests/integration/test_issue310_research_controller.py`: default retained-sufficient controller completion must emit no draft/citation synthesis stages, persist `completion-provenance-v2`, bind `research_runs.answer_sha256` to the public handoff-authority digest, and exclude internal identity keys from the public handoff.
- existing acquisition-authority suites continue to own failed-preflight-before-network and idempotent provider persistence.
- existing self-synthesized completion-provenance suites remain unchanged in substance and verify that the full semantic artifact path still fails closed.

## Intentionally retained historical vocabulary

`shadow_comparison.py` / `test_shadow_comparison.py` retain the name `fsearch_smart_legacy_policy` for a simulated historical benchmark policy. That symbol is not a live command owner and is intentionally not renamed as part of #314.

Historical release/gate records may describe prior command surfaces; they are evidence about those revisions, not current runtime instructions.

## First exact-head host validation and follow-up remediation

The first complete host attempt reached exact candidate
`dfafcf3f276b04af2ca9320fdc11b8a7e9ca0b94` through the operational guard's
supported `HEAD_SHA=<40hex>` authority declaration and detached-head proof.
Identity, merge-base, cleanliness, and final disposable-service cleanup all
passed, but `HOST_VALIDATION=FAIL`; `GATE_DECISION=NOT_EVALUATED` remained
correct.

### Blocking defects discovered

- Ruff 0.16.4 reported five lint findings and thirteen candidate-touched files
  requiring formatter normalization. The five lint findings were repaired in
  the Central follow-up; CI now pins Ruff 0.16.4 so its default rules cannot
  drift independently of the validated local authority. Ruff formatting must
  be re-proved on the new exact head before PR creation.
- Ten of eleven PostgreSQL-backed issue-310 controller tests failed because
  `PostgresCoverageRepository.get_coverage_summary()` queried nonexistent
  `coverage_snapshots.status_counts`, `type_counts`, and `overall_status`
  columns. The authoritative migration and the snapshot writer have always
  stored one immutable JSONB `ledger`. The reader now loads `coverage_revision,
  ledger`, validates the ledger shape, derives status/type counts from its
  items, and uses its persisted overall status. No DDL or migration was added.
  Unit regression coverage now proves both ledger-derived summaries and
  fail-closed malformed-ledger behavior.

### Important non-blocking/harness findings resolved

- The local repository had an unrelated ignored Python 3.14 `.venv`; bare
  Pyrefly and script subprocesses could discover it. Manual validation now
  names `.venv-research-store` executables explicitly and always passes
  `--python-interpreter-path .venv-research-store/bin/python` to Pyrefly.
- `scripts/fixtures/workflow_test_cases.py` now places the currently executing
  test interpreter's bin directory ahead of ambient `PATH` (while retaining the
  fake Firecrawl CLI first). Script fixtures therefore inherit the same Python
  environment as the pinned pytest process instead of an unrelated `.venv`.
- CI Ruff installation and the issue-310/314 exact-head controller-review
  workflow are fixed at `ruff==0.16.4`; an unreviewed future Ruff upgrade is a
  tooling-policy change rather than silent validation drift.

### Test/documentation gaps resolved

- `tests/contract/test_workflow.py` no longer wildcard-imports the historical
  `scripts/fixtures/workflow_test_cases.py` test corpus. Only the current
  `fake_cli` fixture and `run_script` helper are imported, so superseded
  `fsearch_smart --dry-run`/scratch tests cannot silently become active current
  contracts. The current test still proves that `fsearch_smart` exposes no
  diagnostic dry-run surface and is the canonical delegate.
- The workflow-state contract test now checks the semantic low-level-reopen
  statement without requiring one obsolete exact sentence. The canonical
  documentation already states that explicit low-level reopen is specialist
  behavior and not the normal response to a completed controller run.
- The acquisition/controller ownership regression no longer depends on one
  source string spanning one physical line. It parses the acquisition service,
  requires `_resolve_authority_context()` to fail with
  `AcquisitionPreflightError`, and verifies that authority resolution occurs
  before the provider adapter's `search()` call. Existing service-backed
  acquisition-authority tests remain the behavioral authority.
- `tests/contract/test_issue314_handoff_skill_surface.py` now protects the
  deterministic/manual-validation boundary by requiring the sanctioned local
  assessment runner and explicit `.venv-research-store`/Pyrefly interpreter
  binding to remain documented.

## Host validation status after follow-up source changes

The `dfafcf3f...` host evidence is now historical diagnostic evidence only.
Every follow-up source/test/documentation commit changes the candidate identity,
so all acceptance evidence must be regenerated against the new exact branch
head before PR creation. The next local handoff must use the new 40-character
SHA in the guard authority declaration and then run the repository's exact
static, focused, disposable-service, broad-regression, and cleanup authorities.

The local agent remains restricted to host-bound execution, diagnostics,
evidence collection, and narrowly mechanical lint/format repairs. Substantive
production/test semantics remain Central-owned; failures must be returned as
evidence rather than bypassed by weakening tests, guards, schemas, migrations,
or authority checks.

## Second exact-head host validation: retained packet coverage snapshot

Exact-head validation at
`c8c3466474f54d316a29d6dec980c84f2fddae22` passed Ruff, Ruff formatting,
explicitly interpreter-bound Pyrefly, and the 240-test focused non-service
suite. The disposable service-backed phase then produced eight coherent
failures in `tests/integration/test_issue310_research_controller.py`: every
retained-sufficient/restart path reached a persisted completed lifecycle but
returned public disposition `blocked` instead of `terminal_completed`.

The failure was a real authority-boundary defect, not an environment problem.
The retained-first controller path created coverage items and rebuilt the
coverage projection but bypassed the normal orchestrator `CorpusReviewStage` /
`CoverageReviewStage` snapshot materialization. Terminal completion provenance
does not depend on a coverage snapshot, so the run could correctly commit
`completed`; the subsequent issue-314 public handoff intentionally requires the
immutable coverage snapshot at the EvidencePacket's exact coverage revision and
therefore failed closed.

`RetainedReviewService._prepare_evidence()` now materializes the rebuilt
pre-packet `CoverageLedger` as an immutable snapshot at exactly the revision
passed into `EvidencePreparationService`. The snapshot is created before the
EvidencePacket, preserving packet-to-coverage temporal authority rather than
weakening `_build_public_handoff()` or selecting a later mutable projection.
`coverage_snapshots` remains the single persisted snapshot representation; no
DDL or migration change is required.

The PostgreSQL-backed retained-sufficient regression now additionally proves
that the latest EvidencePacket's `coverage_revision` resolves to an exact
persisted coverage snapshot, that the snapshot belongs to the same run, and
that the public handoff exposes that same revision. The prior `c8c3466...`
service-backed failure evidence remains historical diagnostic evidence only;
all acceptance evidence must be regenerated against the new exact candidate.

## Third exact-head validation: broad-suite contract drift

Exact-head validation at `74b45385086cd83728b798ef9a991572f906a136`
proved the retained-snapshot remediation: static gates passed, the 240-test
focused suite passed, and all 83 disposable service-backed regressions passed,
including retained/restart completion, zero provider calls, exact packet-to-
coverage snapshot binding, host-handoff provenance, self-synthesized provenance,
and acquisition-preflight authority.

The first broad-suite attempt used pytest's default prepend import mode and
failed collection on duplicate unit/integration test-module basenames. That was
a handoff artifact: the repository's deterministic assessment runner already
owns `--import-mode=importlib`. The corrected broad invocation collected all
3519 tests and exposed eight repository-contract failures that were outside the
retained-snapshot production path.

Those failures were stale global contracts rather than reasons to restore
retired normal-agent choreography. Current `SKILL.md` intentionally remains
controller-first, while explicit `frun`/`fsearch`/`fscrape` sequences belong in
specialist references such as `curated-run-lifecycle.md` and the operations
runbook. Documentation tests are therefore bound to the appropriate specialist
reference instead of requiring low-level lifecycle syntax in the normal-agent
skill surface. The local validation contract now also states the runner-owned
pytest import policy explicitly, and README manual fallback uses
`--import-mode=importlib` rather than the structurally invalid default broad
collection command.

The issue-297 retrieval stdout test double is updated to honor the current
`fetch-passages` identity-validation boundary: PostgreSQL chunk-identity
resolution is mandatory even when no run-scoped retrieval event will be logged.
Production retrieval behavior is unchanged. The issue-268 topology contract no
longer freezes an obsolete exact count of active test files; it verifies the
stable structural invariant that active tests live only under the canonical
unit, integration, contract, and acceptance roots. Run-level blob-verifier
status/exit semantics are documented in the operator runbook rather than
re-expanded into the normal-agent `SKILL.md` surface.

These contract/documentation changes advance the candidate SHA, so the
`74b4538...` broad-suite result remains diagnostic evidence and the final
acceptance cycle must run against the new exact head.
