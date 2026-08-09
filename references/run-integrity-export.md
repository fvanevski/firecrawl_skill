<!-- @format -->

# Run Integrity and Offline Export Contract

Issue #221 / ARC-15 defines the supported offline audit surface for one research run. PostgreSQL remains authoritative for lifecycle, acquisition provenance, immutable artifacts, staged promotion, exact completion membership, durable index jobs, synthesis provenance, and terminal decisions. Qdrant is a rebuildable projection and is never used by this export as lifecycle or exact-membership authority.

## Commands

```bash
scripts/research-db export-run 'fr_<uuid>' --output run.json
scripts/research-db export-run 'fr_<uuid>' --schema-version export-run-v1 --output legacy-run.json
scripts/research-db integrity 'fr_<uuid>' --output integrity.json
scripts/frun integrity 'fr_<uuid>' --output integrity.json
```

`export-run` defaults to `export-run-v2`; the only supported compatibility version is `export-run-v1`. `integrity` supports `integrity-v1`. Unknown schema labels are rejected by argument parsing rather than copied into an artifact.

`export-run-v1` preserves the historical minimal `{schema_version, run, retrieval_events}` presentation contract. `export-run-v2` and `integrity-v1` use the complete bounded contract below. Presentation exports omit the transaction timestamp so a fixed PostgreSQL state remains byte reproducible. Integrity artifacts retain the snapshot observation timestamp because it is audit evidence.

## One PostgreSQL snapshot

Every v2/integrity artifact is assembled inside one `REPEATABLE READ, READ ONLY` PostgreSQL transaction. The artifact reports the transaction isolation/read-only mode. All lifecycle, provenance, membership, job, synthesis, and terminal-decision sections therefore describe one consistent database snapshot rather than a sequence of independently changing observations.

The exact index census additionally uses the existing single-statement `census_index_jobs()` production seam. It is supplied only the checkpoint/sealed completion membership and persisted index-definition fingerprint. The census never uses Qdrant point counts to classify completion.

## Bounded section envelope

Potentially large collections use this envelope:

```json
{
  "exact_count": 1376,
  "items_limit": 50,
  "items": [],
  "sha256": "<deterministic digest of the complete safe row stream>",
  "truncated": true
}
```

`exact_count` is exact even when `items` is truncated. `sha256` covers the complete recursively redacted/bounded row stream in deterministic query order. Nested lists are separately bounded at 25 entries and long strings at 2,000 characters; their envelopes retain exact length/count plus deterministic SHA-256. The default limits are emitted in the artifact.

The artifact includes bounded run-scoped sections for lifecycle transitions, terminal decisions, execution-mode history, promotion subjects/events, checkpoint observations, invocations, search plans/queries/responses/candidates/occurrences, retrieval events, run assets, sources, snapshots, documents/derivations/blocks/chunks, ingestion batches/assets, semantic calls/artifacts, synthesis stages, evidence packets, claims/evidence links, exact index jobs/manifests/leases/relevant heartbeats, and referenced blob metadata.

## Secret and filesystem redaction

Redaction is recursive across the complete artifact, not limited to duplicate blob-reference fields. Common credential-bearing mapping keys, query parameters, assignments, Bearer tokens, URI user-info passwords, and user home-directory paths are sanitized before hashing or serialization. This includes `asset_snapshots.raw_blob_uri`, nested JSON metadata, error/diagnostic text, lease-related metadata, and semantic/search records.

`integrity` never prints the artifact to stdout. Stdout contains only a bounded write acknowledgement with the output path, schema version, run ID, and overall diagnostic status. Tests seed presigned-URI credentials and nested authorization data and assert that neither the full file nor stdout contains the secrets.

## Exact run membership and indexing

For post-0040 runs, the active `run_asset_membership_seals` row and ordered `run_asset_membership_members` are the exact completion-membership authority. The export verifies asset/chunk counts and compares the active seal's exact chunk set to its bound indexing checkpoint. Index jobs, manifests, leases, and relevant heartbeats are selected only for that exact set.

A pre-0040/compatibility checkpoint may provide exact checkpoint entity IDs without an asset-membership seal. The artifact can still census those persisted IDs, but the membership diagnostic is explicitly `inconclusive` with reason `legacy_checkpoint_without_asset_membership_seal`; it never fabricates historical promotion provenance.

No run membership exists merely because a source has a registered domain, a document exists globally, or a Qdrant point is present. Unrelated runs sharing sources, workers, or index definitions are excluded from the per-run job/lease evidence.

## Terminal decision and late completion evidence

The latest persisted `terminal_decisions.state_census` is preserved verbatim (subject to recursive redaction/bounding) as the authoritative evidence of what the terminal command knew. Later job completion timing is separately derived only from the exact run membership and persisted index fingerprint.

For each exact-member job completed after the terminal decision, the artifact records bounded job timing and exact counts. `spanning_terminal_decision_exact_count` counts jobs whose persisted `started_at <= terminal_decision.created_at < completed_at`.

The historical terminal census stores class counts and bounded representatives, not a guaranteed complete ID list for every running-live member. Therefore the artifact **does not infer** which later job IDs must have been among a historical `running_live` count. `historical_identity_correlation` is explicitly `inconclusive` with reason `terminal_census_does_not_persist_full_running_live_id_set`. This preserves the audited root cause without inventing provenance.

The audited 1,376-member scenario is covered at both layers: the exact census regression constructs 1,344 `complete` plus 32 `running_live` members with zero `claimable`; the integrity regression persists that exact terminal census and proves the offline artifact preserves it. A separate PostgreSQL production-seam regression demonstrates a real running-live member at terminal decision that later completes.

## Diagnostic domains and fail-closed semantics

`integrity-v1` reports an overall status derived from structured domains. Each domain emits `pass`, `failure`, or `inconclusive` plus a machine-readable `reason_code`.

- `membership`: validates active seal counts and checkpoint/set binding; legacy/no-membership history is inconclusive.
- `indexing`: uses the exact PostgreSQL census. Any non-complete class is a failure; unavailable exact census is inconclusive.
- `terminal_decision`: fails when the persisted terminal census contains non-complete index work; absent/unstructured historical census is inconclusive.
- `search_provenance`: persisted `historical_unresolved` or `unresolved_compatibility` responses are reported as inconclusive rather than reconstructed.
- `synthesis_completion`: a `completed` run must at minimum expose persisted evidence packets, semantic calls/artifacts, and synthesis stages. Missing persisted provenance is a failure. Presence is diagnostic evidence only; the production completion guard remains authoritative for semantic validity, current packet binding, hashes, claim/evidence coverage, and external/provisional rejection.

A failure outranks inconclusive; inconclusive outranks pass. The export itself never mutates lifecycle state or satisfies a completion gate.

## Qdrant reconciliation boundary

The offline export records the persisted index definition, PostgreSQL manifest totals, and cached point-count observations when available. Its reconciliation status is intentionally `inconclusive` with `authoritative_for_completion=false` because it does not perform a live Qdrant read. Operators may run the normal reconciliation tooling separately, but neither live nor cached Qdrant point counts become lifecycle or exact-membership authority.

## Compatibility and rollback

This change adds no database schema migration. It consumes the existing through-0044 persisted authority model. Source rollback does not require a database downgrade. `export-run-v1` remains available for consumers that require the prior minimal schema; v2/integrity consumers must honor bounded section envelopes and diagnostic reason codes.

## Verification matrix

Issue-specific validation is in `scripts/test_explicit_export_reproducibility.py`, with exact census classification in `scripts/test_index_census.py`. Required repository validation remains:

```bash
rtk proxy ruff check .
rtk proxy ruff format --check .
rtk proxy env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider scripts/
```

PostgreSQL integration coverage specifically verifies supported schema versions, v1/v2 reproducibility, read-only repeatable-read execution, section bounds and exact counts, cross-run isolation, full-artifact redaction, persisted execution-mode history, exact running-live/later-completion evidence, and the audited 1,344+32 terminal census.
