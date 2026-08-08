# Authoritative completion provenance

Issue #218 makes `completed` an evidence-bearing state, not an operator assertion. A run may enter `completed` only when PostgreSQL can reproduce the exact source membership and immutable synthesis artifacts that were validated.

## Authority chain

For a completed run, the completion gate derives authority from persisted PostgreSQL records in this order:

1. the active `run_asset_membership_seals` row and its exact `run_asset_membership_members` snapshot/chunk membership;
2. the latest `evidence_packets` revision, whose included and omitted passages/snapshots must all resolve within that sealed membership; the packet itself is hashed and recorded as the exact synthesis input, while the seal hash independently identifies the complete source membership;
3. persisted `research_claims` and append-only `claim_evidence_links` for every claim in the packet;
4. current, completed `outline`, `binding`, `draft`, `citation_pass`, and `validation` synthesis stages for that same packet revision;
5. run-local immutable semantic calls and semantic artifacts for the final `draft` and `citation_pass`, including provider/model identity, model revision, prompt version, schema version, semantic-input SHA-256, artifact SHA-256, and the exact EvidencePacket input reference;
6. deterministic validation of the exact immutable citation artifact, including a current packet revision, `validation_status=valid`, `stale_packet=false`, `is_complete=true`, zero validation errors, zero validation warnings, and an evidence-bound claim manifest.

Qdrant is not consulted for lifecycle authority or exact completion membership. It remains a projection/index surface only.

The terminal transition ledger records a `completion_provenance` object containing the exact membership seal, EvidencePacket, synthesis/citation/validation stage IDs, semantic call/artifact IDs, and hashes. `research_runs.source_manifest_sha256` is derived from the active sealed membership. `research_runs.answer_sha256` is derived from the immutable final draft semantic artifact. Neither value is accepted from the caller as authority.

## `frun finish`

Normal completion remains:

```bash
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied
```

`frun finish` derives the authoritative source and answer hashes itself. The optional `--source-manifest-sha256` and `--answer-sha256` arguments are assertions only: when supplied they must be canonical 64-character hexadecimal SHA-256 digests and must exactly match the persisted authoritative records. A malformed or mismatched assertion rejects completion.

`--provenance-type authoritative` is likewise only an assertion. Omitting `--provenance-type` does not upgrade external or provisional content: authority is derived from the persisted semantic call/provider and execution-mode policy. Explicit `external`, `provisional`, or other non-authoritative values are rejected for `completed`.

## Fail-closed and concurrency behavior

Any missing UoW, lookup failure, schema mismatch, stale packet, incomplete validation, missing immutable semantic artifact, missing evidence link, digest mismatch, or non-authoritative semantic provider rejects completion. The boundary error reports the failed invariant without exposing credentials or raw provider payloads.

The workflow performs an early read-only preflight for actionable errors. Immediately before terminal commit, `GuardedResearchRunService` locks the run and exact mutable provenance rows through the authoritative provenance reload, compares the reloaded identities and hashes byte-for-byte with the preflight metadata, and then applies the existing lifecycle/idempotency CAS in the same transaction. Consequently a concurrent provenance change is either included in the final revalidation or cannot cross the terminal commit unnoticed. Identical terminal retries retain the existing idempotent replay behavior.

The immutable semantic artifacts and terminal transition ledger remain the historical authority; mutable synthesis-stage rows are revalidated and locked at terminal commit and are not themselves a substitute for the immutable artifacts recorded there. An explicit reopen clears the run's completion hashes and invalidates prior semantic artifacts before a new lifecycle can produce a new completion record.

## External, provisional, and historical output

Imported, out-of-band, cached, or historical output cannot satisfy completion merely by supplying a hash or omitting a provenance label. It must already be represented through a provenance-preserving semantic operation that records the run-local semantic call, immutable artifact, exact input packet, authority, schema/prompt/model metadata, and validation result required above. The completion gate never fabricates or retroactively infers missing historical provenance.

Existing terminal rows are not rewritten or upgraded by this change. A nonterminal historical run that lacks the required persisted provenance remains resumable, partial, or fail-able, but cannot be promoted to `completed` until it produces new authoritative synthesis provenance.

## Partial and failed semantics

`partial` is reserved for an intentional policy-approved incomplete research result from the permitted lifecycle state. It does not claim authoritative completed synthesis and therefore does not require the completed-run provenance chain.

`failed` represents a terminal failure and likewise does not require synthesis provenance. Infrastructure lag or a recoverable checkpoint is not converted into `partial`; those conditions remain nonterminal/recoverable until policy explicitly determines a terminal outcome.

## Audit and recovery

For a completed run, inspect the `completed` `research_run_transitions.validation_result->'completion'->'completion_provenance'` object together with `research_runs.source_manifest_sha256` and `answer_sha256`. The recorded IDs and hashes are sufficient to locate the exact PostgreSQL membership, EvidencePacket, semantic calls, immutable artifacts, and validation record used for completion.

If completion fails because provenance is stale or incomplete, do not edit terminal metadata or manufacture replacement hashes. Re-run the appropriate evidence/synthesis/validation stage against the current sealed membership and EvidencePacket, then retry `frun finish` with the same stable idempotency key when appropriate.
