# Audited regression baseline

## Purpose

This release-candidate baseline preserves the audited failure sequence as a
permanent regression set. RC-01, RC-02, RC-04, RC-08, RC-09, RC-11, RC-16,
and RC-17 are now ordinary passing regressions after issues #208, #209, #212,
#213, #217, #219, and #220 added the exact PostgreSQL index-job census, the
lease-aware drain barrier, explicit direct-acquisition lifecycle boundaries,
provider no-result normalization, separation of lifecycle-stage telemetry from
provider search responses, exact constituent batch timing, explicit run-level
blob-verification outcomes, and independent doctor diagnostic domains.

There are currently no strict expected failures in the dedicated audit
allowlist. The expected-failure policy below remains the contract for any future
audited defect added before its remediation lands.

The audited indexing fixture is exact:

```text
1,344 complete + 32 running-live = 1,376 total
```

RC-01 and RC-02 deliberately use different production boundaries. RC-01
exercises the exact index-job census implemented by issue #208. RC-02 exercises
the lease-aware drain barrier implemented by issue #209 and records that the
final 32 manifest completions appear in the observation immediately following
the first observation with no newly claimed work. RC-04 exercises the
acquisition preflight, direct wrapper boundary, and normal finish boundary
remediated by issue #212. RC-08 exercises the provider-response parser against
the exact audited plaintext payload and supported JSON empty-result envelopes.
RC-09 exercises the public orchestrator stage-execution boundary and verifies
that lifecycle execution does not write provider-response rows. RC-11 exercises
the exact ingestion-batch timing repository seam. RC-16 exercises
`ResearchRunService.verify()` directly and verifies that zero eligible
path/hash pairs produce an explicit inconclusive result rather than a positive
integrity assertion. RC-17 exercises the canonical doctor diagnostic service
and verifies that referenced integrity, global orphan inventory, projection,
workers, PostgreSQL authority, durable index jobs, and environment connectivity
remain independent domains.

## Finding map

| Finding | Regression test | Frozen required behavior | Remediation status |
|---|---|---|---|
| RC-01 | `test_rc_01_exact_index_job_census_preserves_sealed_membership` | The real worker/census boundary reports a mutually exclusive, count-conserving census for the sealed 1,376-member set: 1,344 complete, 32 running-live, zero claimable, and zero in every other census class. | #208 remediated; ordinary passing regression |
| RC-02 | `test_rc_02_drain_reobserves_final_32_completions` | The real drain boundary reobserves state after the first `claimed=0`; the next observation contains the final 32 completions. | #209 remediated; ordinary passing regression |
| RC-04 | `test_rc_04_direct_acquisition_obeys_lifecycle_boundaries` | A `created` run is rejected consistently by the authoritative acquisition preflight and direct wrapper operation, and the normal finish boundary cannot bypass preparation, start, persistence, or revision transitions. | #212 remediated; ordinary passing regression |
| RC-08 | `test_rc_08_provider_declared_no_results_are_empty` | A valid provider-declared no-result payload is an empty successful search, not a provider failure. | #213 remediated; ordinary passing regression |
| RC-09 | `test_rc_09_stage_execution_does_not_write_provider_response` | Executing a lifecycle stage does not call `record_search_response()` or create provider-response records; lifecycle telemetry must use a separate persistence channel. | #213 remediated; ordinary passing regression |
| RC-11 | `test_rc_11_batch_completion_uses_exact_constituent_start_and_terminal_times` | Batch timing is derived from exact constituent extraction attempts, excluding nonterminal attempts and unrelated batches. | #217 remediated; ordinary passing regression |
| RC-16 | `test_rc_16_zero_blob_verification_is_inconclusive` | Zero eligible or referenced blobs produce an inconclusive result, never a positive integrity proof. | #219 remediated; ordinary passing regression |
| RC-17 | `test_rc_17_orphans_do_not_fail_referenced_blob_integrity`; `tests/unit/test_issue_220_doctor_diagnostics.py` | Unrelated orphan inventory is reported separately and does not fail healthy referenced-blob integrity; doctor exposes seven independent domains and distinct actionable connectivity reason codes. | #220 remediated; ordinary passing regression |

Passing controls cover exact membership conservation, valid nonempty and empty
provider responses, distinct malformed/contract-breaking/provider-error
classification, the remediated census and drain barriers, direct-acquisition
lifecycle boundaries, lifecycle-stage telemetry separation, exact batch timing,
run-verifier zero-object behavior, and the RC-17 doctor domain/classification
contract.

## Remediation-fidelity requirements

Each regression test must invoke the production boundary responsible for the
corresponding remediation issue. A parser, constant, broad SQL-shape check, or
neighboring orchestration behavior is not a substitute for the responsible
producer, repository, lifecycle, persistence, or diagnostic seam.

Mocks and fakes remain acceptable when they are deterministic and
boundary-faithful:

- The RC-01 repository fake supplies the audited sealed entity set and exact
  census classes while the real `IndexWorker.run_batch()` boundary is executed.
- The RC-02 runner supplies two exact worker/census observations while the real
  `drain_index_jobs()` boundary performs bounded waiting and re-observation.
- The RC-04 fixtures execute the real authoritative preflight, direct-operation
  service, and finish boundary while recording lifecycle revision and
  invocation side effects.
- The RC-08 fixture executes the production search-response parser using the
  exact retained `No results found.\n` bytes and supported JSON envelopes.
- The RC-09 fixture executes the public
  `ResearchOrchestrator._execute_stage()` producer and observes
  provider-response writes directly.
- The RC-11 relational fake models batch membership, batch assets,
  snapshot-to-attempt linkage, terminal-state filtering, unrelated rows, and
  the exact timestamp aggregation. It does not accept SQL merely because it
  contains `MAX`, `GREATEST`, or a datetime parameter.
- The RC-16 fixture executes `ResearchRunService.verify()` with an empty
  invocation set and a real `ContentAddressedBlobStore`, proving the zero-object
  behavior at the production verifier boundary rather than at the neighboring
  doctor/blob-health boundary.
- The RC-17 issue-specific suite executes the typed doctor diagnostic service
  used by `scripts/research-db doctor`, with deterministic PostgreSQL, worker,
  Qdrant, Valkey, embedding, reranker, and blob seams. It verifies the complete
  seven-domain result rather than only the classifier or `_blob_health()` helper.
  It also freezes the thin launcher and destructive-reset consumer contracts.

A remediation is complete only when the corresponding assertion passes because
the responsible production behavior changed. A neighboring fix must not make a
baseline test pass under the wrong issue gate.

### RC-01 worker result and scope contract

A scoped `IndexWorker.run_batch(..., entity_ids=...)` call validates a nonempty
active fingerprint before opening a unit of work or performing any heartbeat,
claim, lease, embedding, Qdrant, or completion side effect. Invalid scoped
authority therefore fails closed.

Worker operation counters and census observations have different aggregation
semantics:

- `complete`, `failed`, `lease_lost`, and embedding telemetry remain
  per-invocation deltas and may be summed across worker batches.
- `complete_manifests` is the census-wide completed-manifest total for the
  sealed set at that observation.
- `census.complete` is the same census-wide total inside the complete structured
  observation.
- callers must not sum `complete_manifests` or any census class across
  observations.

The dedicated census suite exercises both worker paths for pre-side-effect
fingerprint rejection, verifies zero-claim census attachment, and simulates
multiple claimed batches followed by a zero-claim observation to prove that
cumulative census totals do not overwrite or inflate operation and telemetry
deltas. Test-module import-path setup is owned by `scripts/conftest.py`, so the
census test keeps its production imports at module scope without a test-local
`sys.path` mutation or an E402 suppression.

### RC-02 drain-barrier contract

Lifecycle draining seals the run's current PostgreSQL chunk membership once and
executes scoped worker batches against that set. A zero-claim observation is
diagnostic only: completion requires a count-conserving census with
`complete == expected` and every recoverable and irrecoverable non-complete
class equal to zero.

The barrier waits with bounded exponential backoff when live or
not-yet-claimable recoverable work remains, permits subsequent scoped batches to
reclaim expired leases and retry retryable failures within configured attempt
limits, and fails closed on dead, missing-job, wrong-fingerprint, or
manifest-inconsistent states. Deadline, batch-bound, and cancellation exits are
structured, recoverable, and explicitly nonterminal; this issue performs no
lifecycle transition or durable checkpoint write.

### RC-04 direct-acquisition lifecycle contract

Direct `fsearch` and `fscrape` operations are legal only while the run is in
`acquiring`. A direct wrapper start never performs a lifecycle transition, and
the acquisition preflight independently enforces the same state boundary before
provider execution. `frun prepare` is the explicit idempotent transition into
that state.

The invocation start transaction holds the run row with `FOR SHARE` while it
records both the exact lifecycle state and revision in invocation metadata and
the append-only start event. `frun seal-acquisition` is the explicit curated
transition out of acquisition and into exact PostgreSQL membership sealing and
indexing. No direct command invokes autonomous candidate expansion.

### RC-08/RC-09 search and lifecycle telemetry contract

The immutable raw provider payload is stored before classification. The parser
recognizes the exact audited plaintext no-result marker and supported JSON
empty-result envelopes as `empty` with `result_count=0`; malformed or unknown
contracts remain `parse_error`, while HTTP or provider-declared failures remain
`provider_error`. The existing four search-response statuses are unchanged.

Result aliases are resolved in the established precedence order
(`data`, `results`, `candidates`, `items`), but an unusable earlier alias does
not mask a later supported collection. This preserves the pre-#213 compatibility
path in which, for example, `data: null` can fall through to a valid `results`
list. Provider-declared no-result envelopes use a stricter rule: every declared
result alias must itself be a supported empty collection. A nonempty secondary
collection, or an unusable declared collection beside the no-results marker,
makes the envelope contract-breaking and therefore `parse_error`; candidate
material can never be silently discarded as an empty success.

`tests/unit/test_issue_213_search_empty_telemetry.py` freezes those cross-alias
contracts together with invocation-status behavior. It is executed by both the
dedicated audit-regression workflow and the authoritative-fsearch workflow, so
the issue-specific regression file cannot drift outside CI while the RC-08 and
RC-09 production-seam tests remain in `test_audit_regression_baseline.py`.

The public orchestrator stage boundary records duration through structured
logging and relies on each stage's existing PostgreSQL lifecycle transitions and
append-only events. It does not synthesize `stage:*` provider queries or write
stage messages through `record_search_response()`.

### RC-16 run-verifier contract

`ResearchRunService.verify()` examines only invocation-output `snapshot` and
`artifacts` values that contain both a `path` and expected `sha256`. Each such
eligible pair contributes to exactly one integrity class: `available` when the
content-addressed blob exists and verifies, `missing` when the expected digest
is absent from `BLOB_ROOT`, or `hash_mismatch` when the digest path exists but
its bytes do not hash to the expected value. Therefore
`total == available + missing + hash_mismatch` for every report.

The structured status is `inconclusive` when `total == 0`, `failed` when any
eligible pair is missing or hash-mismatched, and `passed` only when at least one
eligible pair was examined and all are available. File-only legacy paths remain
`file_based_unverified`; they do not create a positive integrity proof and do
not make a zero-eligible report conclusive.

For backward compatibility, the CLI exit code is not itself the integrity
verdict. Conclusive `passed` and `failed` reports retain exit code `0`;
`inconclusive` exits `1` by default; and `--allow-empty` changes only an
inconclusive result to exit `0`. Automation that needs integrity truth must read
the JSON `status` and detailed counters rather than infer it from exit code
alone.

`tests/unit/test_issue_219_verifier_inconclusive.py` covers zero eligible objects,
all-valid objects, a referenced-but-absent blob, a present-but-corrupt blob,
mixed classes, legacy file-only references, and the stable CLI exit-code
mapping. The dedicated audit-regression workflow executes this suite on Python
3.11 and 3.12.

### RC-17 doctor diagnostic contract

`doctor-diagnostics-v1` exposes `postgres_authority`,
`referenced_blob_integrity`, `unreferenced_blob_inventory`,
`index_job_health`, `qdrant_projection`, `worker_health`, and
`environment_connectivity` as separate domains. Every domain uses
`pass`, `warning`, `failure`, or `inconclusive`. A global orphan warning cannot
invalidate healthy referenced-blob integrity, while a missing or corrupt
referenced blob remains a failure.

Connectivity classification combines exception type/errno with component
context. Sandbox policy denial, namespace/routing failure, unavailable server,
credential/configuration failure, PostgreSQL rejection, and query/runtime
failure have distinct machine-readable reason codes and remediation text.
Diagnostic detail is bounded and redacts common credential forms. Qdrant remains
a rebuildable projection: exact point coverage never clears an embedding or
schema compatibility failure and never becomes lifecycle or membership
authority.

`tests/unit/test_issue_220_doctor_diagnostics.py` exercises the typed production
service and canonical shell route, including human/JSON category parity,
adversarial `ECONNREFUSED`/`errno111` forms, PostgreSQL privilege rejection,
secret redaction, Qdrant failure monotonicity, and the reset-script clean-state
consumer. `references/doctor-diagnostics.md` is the normative operator contract.

## Strict expected-failure policy

An unresolved audited defect uses
`pytest.mark.xfail(strict=True, raises=AssertionError, ...)`. The assertion must
fail against the audited production behavior for the stated RC finding. An
underlying production fix therefore creates an unexpected pass and fails this
dedicated workflow until the corresponding remediation PR deliberately:

1. removes the test's `xfail` marker while retaining the now-passing regression
   assertion; and
2. removes the matching entry from
   `references/audit-regression-skip-allowlist.json`.

Issues #208, #209, #212, #213, #217, #219, and #220 have completed that process
for the audited defects currently represented in this baseline. The dedicated
allowlist is therefore empty. It remains isolated from the repository-wide skip
allowlist so any future audited expected failure can be classified and removed
independently without creating stale entries elsewhere.

## Change boundary

The RC-01 remediation added an observational PostgreSQL census and worker result
evidence. The RC-02 remediation added a bounded run-scoped consumer of that
census and a structured resumable command result. The RC-04 remediation added
explicit run-mode metadata, direct-acquisition lifecycle commands, and exact
invocation start provenance using existing PostgreSQL columns and JSONB
metadata. The RC-08/RC-09 remediation adds provider no-result normalization and
keeps lifecycle-stage observability on existing transition/event/logging
surfaces. RC-11 corrects constituent batch timing without inventing provenance.
RC-16 adds a structured run-verifier outcome plus disjoint
available/missing/hash-mismatch accounting over invocation-referenced immutable
blobs. RC-17/#220 adds observational doctor-domain separation, actionable
failure classification, synchronized reset consumption, and diagnostic
redaction without changing database schema, inferring historical provenance, or
consulting Qdrant for authoritative integrity. PostgreSQL remains authoritative,
immutable provider payloads remain in `BLOB_ROOT`, and Qdrant remains a
rebuildable projection.
