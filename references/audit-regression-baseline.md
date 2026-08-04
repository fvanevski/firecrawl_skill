# Audited regression baseline

## Purpose

This release-candidate baseline freezes eight defects observed during the audited run as deterministic strict expected failures. It is intentionally test-only: the tests describe the required corrected behavior while preserving the current defects until their dedicated remediation issues are implemented.

The audited indexing fixture is exact:

```text
1,344 complete + 32 running-live = 1,376 total
```

RC-01 and RC-02 deliberately use different production boundaries. RC-01 exercises the exact index-job census required by issue #208. RC-02 exercises the lease-aware drain barrier required by issue #209 and records that the final 32 manifest completions appear in the observation immediately following the first observation with no newly claimed work.

## Finding map

| Finding | Regression test | Frozen required behavior | Future remediation |
|---|---|---|---|
| RC-01 | `test_rc_01_exact_index_job_census_preserves_sealed_membership` | The real worker/census boundary reports a mutually exclusive, count-conserving census for the sealed 1,376-member set: 1,344 complete, 32 running-live, zero claimable, and zero in every other census class. | #208 |
| RC-02 | `test_rc_02_drain_reobserves_final_32_completions` | The real drain boundary reobserves state after the first `claimed=0`; the next observation contains the final 32 completions. | #209 |
| RC-04 | `test_rc_04_direct_acquisition_obeys_lifecycle_boundaries` | A `created` run is rejected consistently by the authoritative acquisition preflight and direct wrapper operation, and the normal finish boundary cannot bypass preparation, start, persistence, or revision transitions. | #212 |
| RC-08 | `test_rc_08_provider_declared_no_results_are_empty` | A valid provider-declared no-result payload is an empty successful search, not a provider failure. | #213 |
| RC-09 | `test_rc_09_stage_execution_does_not_write_provider_response` | Executing a lifecycle stage does not call `record_search_response()` or create provider-response records; lifecycle telemetry must use a separate persistence channel. | #213 |
| RC-11 | `test_rc_11_batch_completion_uses_latest_constituent_terminal_time` | Batch `completed_at` is the maximum terminal `extraction_attempts.end_time` linked through the exact batch's assets and snapshots, excluding nonterminal attempts and unrelated batches. | #217 |
| RC-16 | `test_rc_16_zero_blob_verification_is_inconclusive` | Zero eligible or referenced blobs produce an inconclusive result, never a positive integrity proof. | #219 |
| RC-17 | `test_rc_17_orphans_do_not_fail_referenced_blob_integrity` | Unrelated orphan inventory is reported separately and does not fail healthy referenced-blob integrity. | #220 |

Two controls remain ordinary passing tests: exact membership conservation and successful classification of a valid nonempty provider response.

## Remediation-fidelity requirements

Each expected-failure test must invoke the production boundary responsible for the corresponding remediation issue. A parser, constant, broad SQL-shape check, or neighboring orchestration behavior is not a substitute for the responsible producer, repository, lifecycle, or persistence seam.

Mocks and fakes remain acceptable when they are deterministic and boundary-faithful:

- The RC-01 repository fake supplies the audited sealed entity set and exact census classes while the real `IndexWorker.run_batch()` boundary is executed.
- The RC-04 fixtures execute the real authoritative preflight, direct-operation service, and finish boundary while recording lifecycle revision and invocation side effects.
- The RC-09 fixture executes the real `ResearchOrchestrator._execute_stage()` producer and observes provider-response writes directly.
- The RC-11 relational fake models batch membership, batch assets, snapshot-to-attempt linkage, terminal-state filtering, unrelated rows, and the exact timestamp aggregation. It does not accept SQL merely because it contains `MAX`, `GREATEST`, or a datetime parameter.

A remediation is complete only when the corresponding assertion passes because the responsible production behavior changed. A neighboring fix must not make a baseline test pass under the wrong issue gate.

## Strict expected-failure policy

Each defect test uses `pytest.mark.xfail(strict=True, raises=AssertionError, ...)`. The assertion must fail against the audited production behavior for the stated RC finding. An underlying production fix therefore creates an unexpected pass and fails this dedicated workflow until the corresponding remediation PR deliberately:

1. removes the test's `xfail` marker while retaining the now-passing regression assertion; and
2. removes the matching entry from `references/audit-regression-skip-allowlist.json`.

The allowlist is isolated from the repository-wide skip allowlist so each remediation can remove its classification independently without creating stale entries elsewhere.

## Change boundary

This baseline changes no production behavior, database schema, migration, durable authority boundary, or public API. PostgreSQL remains authoritative, immutable provider payloads remain in `BLOB_ROOT`, and Qdrant remains a rebuildable projection.
