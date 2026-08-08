# Authoritative completion provenance

Issue #218 makes `completed` an evidence-bearing state rather than an operator assertion. A run may enter `completed` only when PostgreSQL can reproduce the exact source membership and immutable synthesis artifacts that were validated, and that authority is frozen while the run remains terminal.

## Authority chain

For a completed run, the completion gate derives authority from persisted PostgreSQL records in this order:

1. the active `run_asset_membership_seals` row and its exact `run_asset_membership_members` snapshot/chunk membership;
2. the latest `evidence_packets` revision, whose included and omitted passages and snapshots must all resolve within that sealed membership;
3. persisted `research_claims` and `claim_evidence_links` for every claim required by the packet;
4. current, completed `outline`, `binding`, `draft`, `citation_pass`, and `validation` synthesis stages for the same EvidencePacket revision;
5. run-local immutable semantic calls and artifacts for the final `draft` and `citation_pass`, including provider/model identity, model revision, prompt version, schema version, semantic-input SHA-256, artifact SHA-256, and the exact EvidencePacket input reference;
6. deterministic validation of the exact immutable citation artifact, including current packet revision, `validation_status=valid`, `stale_packet=false`, `is_complete=true`, zero validation errors, zero validation warnings, and an evidence-bound claim manifest.

Qdrant is not lifecycle authority and is not consulted to establish exact completion membership. It remains a projection/index surface.

The completed transition records a `completion_provenance` object containing the exact membership seal, EvidencePacket, draft/citation/validation stage IDs, semantic call/artifact IDs, provider/model/prompt/schema identities, and hashes. `research_runs.source_manifest_sha256` is derived from the sealed source membership. `research_runs.answer_sha256` is derived from the immutable final draft semantic artifact. Neither field is accepted from the caller as authority.

## Terminal service wiring

All repository factories created by `build_run_service()` use `GuardedResearchRunService`. Consequently wrapper completion, curated completion, and direct terminal lifecycle commands share the same terminal-decision transaction and the same authoritative completion revalidation.

For a new `completed` command, the guarded service:

1. locks the `research_runs` row with `FOR UPDATE`;
2. confirms the expected lifecycle revision and permitted prior state;
3. reloads all completion provenance with row locks in the same transaction;
4. compares that authoritative reload byte-for-byte with the preflight completion bundle;
5. records the terminal decision and lifecycle transition and applies the terminal state in that same transaction.

An identical already-committed terminal command remains idempotently replayable. A changed revision, changed provenance bundle, or changed terminal decision is rejected.

## Two-sided concurrency and terminal immutability

Migration `0044_terminal_provenance_guard` installs one database-level writer guard on the completion-critical provenance tables:

- `evidence_packets`
- `research_claims`
- `claim_evidence_links`
- `synthesis_stages`
- `semantic_calls`
- `semantic_artifacts`
- `run_asset_membership_seals`
- `run_asset_membership_members`

Before a row mutation on any guarded table can be accepted, its trigger locks the owning `research_runs` row with `FOR KEY SHARE` and checks the current run state. Terminalization independently locks that same run row with `FOR UPDATE` before final provenance revalidation. Those lock modes conflict, so a provenance write and terminalization cannot both commit across the same run boundary unnoticed.

If a writer establishes the run-side lock and commits first, terminalization waits and then revalidates the writer's committed provenance. If terminalization commits first, a waiting writer resumes, observes the terminal state, and is rejected. For `UPDATE` or `DELETE`, PostgreSQL may already hold a target-row lock before the row-level trigger requests the run lock; if opposite lock acquisition produces a deadlock, PostgreSQL aborts one transaction rather than allowing both mutations to commit. In every ordering, the invariant is fail-closed: terminal provenance cannot change successfully without an explicit reopen.

This database guard is deliberate defense in depth. It protects service methods, raw SQL, future repository paths, and concurrent callers rather than relying on each Python writer to remember a state check.

## Reopen semantics

`completed`, `partial`, `failed`, and `cancelled` runs have immutable completion-critical provenance. The only supported way to revise that provenance is the explicit lifecycle reopen command.

The reopen transaction moves `research_runs.state` back to `created` before invalidating prior semantic artifacts. Because the run is nonterminal at that point, the terminal-provenance trigger permits the invalidation and subsequent new evidence/synthesis writes. Reopen also clears the run's completion hashes under the existing lifecycle semantics. A new completion therefore requires a fresh authoritative provenance chain.

Existing terminal rows are not rewritten or retroactively upgraded.

## `frun finish`

Normal completion is:

```bash
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied
```

`frun finish` derives the authoritative source and answer hashes itself. Optional `--source-manifest-sha256` and `--answer-sha256` values are assertions only. When supplied they must be canonical 64-character hexadecimal SHA-256 digests and must exactly match the persisted authoritative records.

`--provenance-type authoritative` is also only an assertion. Omitting it does not upgrade external or provisional content. Explicit `external`, `provisional`, or other non-authoritative values are rejected for `completed`, and persisted semantic provider/authority must independently match the run's execution mode.

## External, provisional, cached, and historical output

Imported, out-of-band, cached, or historical output cannot satisfy completion merely by supplying a hash or omitting a provenance label. It must already be represented by a provenance-preserving semantic operation that records the run-local semantic call, immutable artifact, exact input packet, authority, schema/prompt/model metadata, and validation result required above.

Cache hits that do not preserve the required run-local semantic call/artifact linkage cannot authorize completion. The completion gate never fabricates or retroactively infers missing historical provenance.

## Fail-closed behavior

Completion rejects any missing transactional UoW, lookup failure, malformed SHA-256, hash mismatch, missing or cross-run semantic linkage, non-authoritative provider, invalid semantic artifact, stale EvidencePacket, missing claim/evidence link, stale synthesis stage, failed citation pass, incomplete validation, validation error, validation warning, or validation hash mismatch.

Provenance-loader and database failures are completion failures. There is no compatibility path that silently bypasses the gate.

## Partial and failed semantics

`partial` is reserved for an intentional policy-approved incomplete research result from the permitted lifecycle state. It does not claim authoritative completed synthesis and therefore does not require the completed-run provenance chain.

`failed` represents a terminal failure and likewise does not require authoritative synthesis. Infrastructure lag or a recoverable indexing/checkpoint condition is not converted into `partial`; those conditions remain recoverable until policy explicitly determines a terminal outcome.

The audited failed run therefore remains accurately represented as having no authoritative synthesis.

## Migration and compatibility

`0044_terminal_provenance_guard` is a forward-only workflow migration, consistent with the existing research-store migration policy. It adds no columns and rewrites no historical provenance. Existing nonterminal runs continue normally; existing terminal runs become protected against new completion-critical provenance mutation.

Rollback is by forward repair or PostgreSQL restore rather than destructive Alembic downgrade. The migration's `downgrade()` intentionally raises, matching the repository's forward-only migration contract.

## Verification

The production-seam tests cover:

- factory wiring to `GuardedResearchRunService`;
- migration installation on every guarded provenance table;
- post-terminal rejection of EvidencePacket, claim insert/update/delete, evidence-link insert/delete, synthesis-stage, semantic-call insert/update, semantic-artifact, and membership mutations;
- explicit reopen restoring legal provenance writes;
- a new EvidencePacket writer blocked behind terminalization that resumes and rejects after terminal commit;
- an `UPDATE synthesis_stages` writer blocked behind the fully locked terminal provenance snapshot that resumes and rejects after terminal commit;
- a writer that commits first and forces final terminal revalidation to fail;
- authoritative hash derivation and binding;
- malformed/mismatched hash rejection;
- missing semantic provenance;
- external/provisional authority rejection;
- stale EvidencePacket rejection;
- incomplete validation rejection;
- missing evidence-link rejection;
- fail-closed provenance-store errors;
- partial/failed semantics and terminal retry behavior.

Both the broad CI matrix and the index-checkpoint matrix run the completion provenance integration suite on Python 3.11 and 3.12.

## Audit and recovery

For a completed run, inspect the `completed` `research_run_transitions.validation_result->'completion'->'completion_provenance'` object together with `research_runs.source_manifest_sha256` and `answer_sha256`. The recorded IDs and hashes locate the exact PostgreSQL membership, EvidencePacket, semantic calls, immutable artifacts, and validation record used for completion.

If completion fails because provenance is stale or incomplete, do not edit terminal metadata or manufacture replacement hashes. Re-run the appropriate evidence/synthesis/validation stage on a nonterminal run, or explicitly reopen a terminal run before producing new authoritative provenance, then retry completion.
