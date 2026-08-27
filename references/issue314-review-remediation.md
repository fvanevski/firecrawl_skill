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

## Host validation status

**UNVERIFIED at this document write.** No Ruff, format, Pyrefly, pytest, disposable PostgreSQL/Qdrant/Valkey, or live-runtime result is asserted here. Host evidence must be gathered against one frozen exact branch SHA after Central source review is complete. Any subsequent source/test/documentation commit invalidates that evidence and requires a new run.

The local agent is restricted to host-bound execution, diagnostics, evidence collection, and narrowly mechanical lint/format repairs. Substantive production/test semantics remain Central-owned; failures must be returned as evidence rather than bypassed by weakening tests, guards, schemas, or authority checks.
