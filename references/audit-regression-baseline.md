# Audited regression baseline

## Purpose

This release-candidate baseline freezes seven remaining defects observed during the audited run as deterministic strict expected failures. RC-01 is now an ordinary passing regression after issue #208 added the exact PostgreSQL index-job census. The remaining entries are intentionally test-only: they describe required corrected behavior while preserving their current defects until the dedicated remediation issues are implemented.

The audited indexing fixture is exact:

```text
1,344 complete + 32 running-live = 1,376 total
```

RC-01 and RC-02 deliberately use different production boundaries. RC-01 exercises the exact index-job census implemented by issue #208. RC-02 exercises the lease-aware drain barrier required by issue #209 and records that the final 32 manifest completions appear in the observation immediately following the first observation with no newly claimed work.

## Finding map

| Finding | Regression test | Frozen required behavior | Remediation status |
|---|---|---|---|
| RC-01 | `test_rc_01_exact_index_job_census_preserves_sealed_membership` | The real worker/census boundary reports a mutually exclusive, count-conserving census for the sealed 1,376-member set: 1,344 complete, 32 running-live, zero claimable, and zero in every other census class. | #208 remediated; ordinary passing regression |
| RC-02 | `test_rc_02_drain_reobserves_final_32_completions` | The real drain boundary reobserves state after the first `claimed=0`; the next observation contains the final 32 completions. | #209 |
| RC-04 | `test_rc_04_direct_acquisition_obeys_lifecycle_boundaries` | A `created` run is rejected consistently by the authoritative acquisition preflight and direct wrapper operation, and the normal finish boundary cannot bypass preparation, start, persistence, or revision transitions. | #212 |
| RC-08 | `test_rc_08_provider_declared_no_results_are_empty` | A valid provider-declared no-result payload is an empty successful search, not a provider failure. | #213 |
| RC-09 | `test_rc_09_stage_execution_does_not_write_provider_response` | Executing a lifecycle stage does not call `record_search_response()` or create provider-response records; lifecycle telemetry must use a separate persistence channel. | #213 |
| RC-11 | `test_rc_11_batch_completion_uses_latest_constituent_terminal_time` | Batch `completed_at` is the maximum terminal `extraction_attempts.end_time` linked through the exact batch's assets and snapshots, excluding nonterminal attempts and unrelated batches. | #217 |
| RC-16 | `test_rc_16_zero_blob_verification_is_inconclusive` | Zero eligible or referenced blobs produce an inconclusive result, never a positive integrity proof. | #219 |
| RC-17 | `test_rc_17_orphans_do_not_fail_referenced_blob_integrity` | Unrelated orphan inventory is reported separately and does not fail healthy referenced-blob integrity. | #220 |

Three controls are ordinary passing tests: exact membership conservation, successful classification of a valid nonempty provider response, and the remediated RC-01 census boundary.

## Remediation-fidelity requirements

Each regression test must invoke the production boundary responsible for the corresponding remediation issue. A parser, constant, broad SQL-shape check, or neighboring orchestration behavior is not a substitute for the responsible producer, repository, lifecycle, or persistence seam.

Mocks and fakes remain acceptable when they are deterministic and boundary-faithful:

- The RC-01 repository fake supplies the audited sealed entity set and exact census classes while the real `IndexWorker.run_batch()` boundary is executed.
- The RC-04 fixtures execute the real authoritative preflight, direct-operation service, and finish boundary while recording lifecycle revision and invocation side effects.
- The RC-09 fixture executes the real `ResearchOrchestrator._execute_stage()` producer and observes provider-response writes directly.
- The RC-11 relational fake models batch membership, batch assets, snapshot-to-attempt linkage, terminal-state filtering, unrelated rows, and the exact timestamp aggregation. It does not accept SQL merely because it contains `MAX`, `GREATEST`, or a datetime parameter.

A remediation is complete only when the corresponding assertion passes because the responsible production behavior changed. A neighboring fix must not make a baseline test pass under the wrong issue gate.

### RC-01 worker result and scope contract

A scoped `IndexWorker.run_batch(..., entity_ids=...)` call validates a nonempty active fingerprint before opening a unit of work or performing any heartbeat, claim, lease, embedding, Qdrant, or completion side effect. Invalid scoped authority therefore fails closed.

Worker operation counters and census observations have different aggregation semantics:

- `complete`, `failed`, `lease_lost`, and embedding telemetry remain per-invocation deltas and may be summed across worker batches.
- `complete_manifests` is the census-wide completed-manifest total for the sealed set at that observation.
- `census.complete` is the same census-wide total inside the complete structured observation.
- callers must not sum `complete_manifests` or any census class across observations.

The dedicated census suite exercises both worker paths for pre-side-effect fingerprint rejection, verifies zero-claim census attachment, and simulates multiple claimed batches followed by a zero-claim observation to prove that cumulative census totals do not overwrite or inflate operation and telemetry deltas. Test-module import-path setup is owned by `scripts/conftest.py`, so the census test keeps its production imports at module scope without a test-local `sys.path` mutation or an E402 suppression.

## Strict expected-failure policy

Each unresolved defect test uses `pytest.mark.xfail(strict=True, raises=AssertionError, ...)`. The assertion must fail against the audited production behavior for the stated RC finding. An underlying production fix therefore creates an unexpected pass and fails this dedicated workflow until the corresponding remediation PR deliberately:

1. removes the test's `xfail` marker while retaining the now-passing regression assertion; and
2. removes the matching entry from `references/audit-regression-skip-allowlist.json`.

Issue #208 performed both steps for RC-01. The allowlist remains isolated from the repository-wide skip allowlist so each later remediation can remove its classification independently without creating stale entries elsewhere.

## Change boundary

The RC-01 remediation adds an observational PostgreSQL census and worker result evidence only. It also enforces the pre-existing requirement that a scoped worker call possess an active fingerprint before any mutation and preserves the established per-batch result contract. It does not change orchestration waiting, retry, terminal behavior, database schema, migration history, or the durable authority boundary. PostgreSQL remains authoritative, immutable provider payloads remain in `BLOB_ROOT`, and Qdrant remains a rebuildable projection.
