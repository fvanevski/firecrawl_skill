# Authoritative ingestion batch semantics

Issue #217 defines the RC-11/RC-12/RC-13 contract for ingestion batches. The
contract is PostgreSQL-authoritative. Qdrant remains a rebuildable projection
and must not supply lifecycle, timing, provenance, or exact membership facts.

## Three distinct timestamps

A v43 batch has three intentionally different timestamps:

- `started_at` is the earliest authoritative constituent start.
- `sealed_at` is when PostgreSQL closes the batch membership.
- `completed_at` is the latest authoritative constituent terminal outcome.

For a member linked to an extraction attempt, `extraction_attempts.start_time`
and `extraction_attempts.end_time` are authoritative. The batch stores the exact
current `extraction_attempt_id`; it never rebinds the content-addressed snapshot
or document to a later retry.

For a direct-ingestion member with no extraction attempt,
`ingestion_batch_assets.constituent_started_at` and
`constituent_completed_at` record the observed persistence interval. A raw
repository-level member recorded without an external attempt uses the recording
instant as a zero-duration constituent event, because that insert is the only
observed direct-member operation at that API boundary.

New v43 finalization fails closed if a member lacks its required start or
terminal timestamp. It never substitutes statement wall clock for missing
constituent terminal evidence. Revision 0042 retains its legacy completion
behavior solely for rolling code/schema compatibility.

## Exact membership and sealing

`record_batch_asset()` and `finish_ingestion_batch()` acquire the same
`ingestion_batches` row with `SELECT ... FOR UPDATE`. Therefore a concurrent
insert and seal have one serial PostgreSQL order:

1. if the insert commits first, it is part of the sealed summary; or
2. if the seal commits first, the later insert observes `sealed_at` and fails.

Retries explicitly reopen a terminal batch, clear the v43 seal and summary, and
replace the reconstructable member ledger in the same transaction. Revision 42
uses a separate SQL shape that never mentions v43-only columns.

## Outcome summary v2

Terminal v43 batches expose `outcome_summary` through the canonical invocation
export. Its schema version is `ingestion-outcome-summary-v2` and its stable
identity type is `ingestion_batch_asset_id`.

The summary contains:

- `member_count`;
- `succeeded`, `succeeded_ids`, and `succeeded_extraction_attempt_ids`;
- `failed`, `failed_ids`, and `failed_extraction_attempt_ids`;
- `cancelled`, `cancelled_ids`, and `cancelled_extraction_attempt_ids`;
- `failure_classes`, where each class has an exact `count` and ordered `ids`;
- `members`, an ordered per-member outcome record.

The ingestion asset status remains the persistence result (`complete` or
`failed`). Cancellation is not overloaded onto that field. For extraction-backed
members it is derived from the exact linked `extraction_attempts.exit_status`.
This allows an ingestion row to remain `failed` while the outcome summary
truthfully distinguishes `failed` from `cancelled` extraction outcomes.

Bounded extraction waves retain terminal preflight attempts as exact batch
members. A failed or cancelled preflight attempt therefore contributes to
batch timing, outcome IDs, cancellation counts, and failure-class membership
rather than disappearing before the corpus manifest is built.

## Canonical reads and rolling compatibility

`export_invocation()` and `export_invocation_by_batch()` use separate v42 and
v43 column/key shapes. On v42, `sealed_at` and `outcome_summary` are explicitly
`None`; the external research-run ID remains under the correct run-ID fields and
is never positionally mislabelled as a v43 column.

V43 asset exports additionally expose `batch_asset_id`, the exact
`extraction_attempt_id`, and direct constituent timing fields. These fields make
the persisted summary independently auditable from PostgreSQL.

## Stage-specific selection semantics

Issue #217 does not create another promotion state machine. It reuses the
existing ARC-05 ledger documented in `asset-promotion-membership.md`.
`AssetPromotionService.list_assets()` exposes these explicit stage facts:

- `selected_for_extraction`;
- `extraction_succeeded` (the public name for having reached ARC-05 `extracted`);
- `retained`;
- `evidence_eligible`;
- `completion_critical`.

For authoritative subjects the flags are derived from the current stage and
append-only promotion events. For historical assets with
`current_stage = unknown` / `provenance = legacy_unstructured`, every flag is
`None`, not `False`, because absence of historical evidence cannot prove that a
stage was never reached.

Legacy storage columns named `selected` remain compatibility fields. New read
semantics are stage-specific: retrieval traces additionally expose
`selected_for_retrieval`, while asset/extraction lifecycle consumers use the
ARC-05 fields above. New code must not interpret a bare `selected` value as
extraction success, retention, evidence eligibility, or completion membership.

## Historical and migration policy

Migration 0043 is additive and does not rewrite existing timestamps or infer
historical constituent/promotion intent. Existing rows keep their prior
`started_at` and `completed_at`; newly added member timing and seal fields begin
unknown until authoritative new work supplies evidence.

Database downgrade is intentionally unsupported: `downgrade()` fails closed.
A source-code rollback is distinct from a database downgrade; database recovery
requires a forward repair or restoration from a PostgreSQL backup.

## Regression coverage

`scripts/test_issue_217_ingestion_batch_semantics.py` covers exact MIN/MAX
constituent timing, fail-closed missing terminal evidence, exact outcome IDs and
failure classes, cancellation, seal/insert serialization, reused-snapshot
provenance, real v42 retry/export compatibility, and ARC-05 stage-specific read
semantics. The audit baseline independently protects the RC-11 exact timing
contract, while the issue #216 production-seam tests verify bounded preflight
behavior remains compatible with complete batch membership.
