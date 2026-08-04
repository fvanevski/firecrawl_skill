# Audited regression baseline

## Purpose

This release-candidate baseline freezes eight defects observed during the audited run as deterministic strict expected failures. It is intentionally test-only: the tests describe the required corrected behavior while preserving the current defects until their dedicated remediation issues are implemented.

The audited indexing fixture is exact:

```text
1,344 complete + 32 running-live = 1,376 total
```

The drain-barrier fixture additionally records that the final 32 manifest completions appear in the observation immediately following the first observation with no newly claimed work.

## Finding map

| Finding | Regression test | Frozen required behavior | Future remediation |
|---|---|---|---|
| RC-01 | `test_rc_01_running_live_prevents_quiescent_success` | `running_live=32` prevents a quiescent success result even when `claimed=0`. | #208 |
| RC-02 | `test_rc_02_drain_reobserves_final_32_completions` | The drain reobserves state after the first `claimed=0`; the next observation contains the final 32 completions. | #209 |
| RC-04 | `test_rc_04_created_state_is_not_acquisition_eligible` | Direct acquisition rejects the `created` lifecycle state. | #212 |
| RC-08 | `test_rc_08_provider_declared_no_results_are_empty` | A valid provider-declared no-result payload is an empty successful search, not a provider failure. | #213 |
| RC-09 | `test_rc_09_stage_marker_is_not_a_valid_search_response` | A lifecycle stage marker such as `{"stage": "planning"}` cannot satisfy the provider search-response contract. | #213 |
| RC-11 | `test_rc_11_batch_completion_uses_latest_constituent_terminal_time` | Batch `completed_at` is the latest terminal timestamp of constituent work, not the wall clock at the batch-finishing statement. | #217 |
| RC-16 | `test_rc_16_zero_blob_verification_is_inconclusive` | Zero eligible or referenced blobs produce an inconclusive result, never a positive integrity proof. | #219 |
| RC-17 | `test_rc_17_orphans_do_not_fail_referenced_blob_integrity` | Unrelated orphan inventory is reported separately and does not fail healthy referenced-blob integrity. | #220 |

Two controls remain ordinary passing tests: exact membership conservation and successful classification of a valid nonempty provider response.

## Strict expected-failure policy

Each defect test uses `pytest.mark.xfail(strict=True, raises=AssertionError, ...)`. The assertion must fail against the audited production behavior for the stated RC finding. An underlying production fix therefore creates an unexpected pass and fails this dedicated workflow until the corresponding remediation PR deliberately:

1. removes the test's `xfail` marker while retaining the now-passing regression assertion; and
2. removes the matching entry from `references/audit-regression-skip-allowlist.json`.

The allowlist is isolated from the repository-wide skip allowlist so each remediation can remove its classification independently without creating stale entries elsewhere.

## Change boundary

This baseline changes no production behavior, database schema, migration, durable authority boundary, or public API. PostgreSQL remains authoritative, immutable provider payloads remain in `BLOB_ROOT`, and Qdrant remains a rebuildable projection.
