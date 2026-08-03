# Release campaign timing diagnostics

`timing-diagnostics.json` is retained release evidence with schema
`release-campaign-timing-v2`. PostgreSQL remains authoritative for research-run
lifecycle, semantic-call identity, status, retries, and timestamps. The JSON is
not runtime state, is never consumed for acquisition or replay, and is never a
file-mediated ingestion manifest.

## Target A authority boundary

- `research_runs` supplies authoritative run identity and lifecycle timestamps.
- `semantic_calls` supplies authoritative call IDs, idempotency keys, statuses,
  attempt metadata, and call timestamps.
- `BLOB_ROOT` remains the immutable content-addressed payload store.
- Qdrant remains a rebuildable projection.
- Valkey remains transient coordination and is not required to interpret or
  recover the timing evidence.

The artifact does not alter transaction boundaries, commit ordering, retry
semantics, or release thresholds. It contains no scratch acquisition path and
must not be used as durable application state.

## Required contract

Each run is bound to its Campaign A or Campaign B descriptor, execution mode,
objective ID, and committed PostgreSQL run UUID. Every semantic-stage aggregate
contains the contributing semantic-call UUIDs and idempotency keys, status
counts, attempt and retry counts, attempt-latency coverage, wall-clock coverage,
and an explicit `telemetry_complete` result.

Missing timing is never represented as zero. Missing or malformed attempt
metadata produces `null` timing plus explicit missing-observation counts. The
same rule applies to absent call timestamps. Failed and retried semantic calls
remain visible in status and attempt counts; their measured latency is included
when the authoritative row contains complete timing observations.

## Comparison binding

Every entry in `comparison.json` `details` must have exactly one corresponding
`reproducibility_failures` record. That record must bind the exact Campaign A
and Campaign B run UUIDs, reproduce the compared values and ratio, and include
the complete paired semantic-stage comparison. Extra, missing, duplicated, or
partially bound failures invalidate the release evidence.

When neither paired run has semantic-call rows, the failure record must state
`not_applicable` and include an explicit reason. When stage rows exist, missing
stages or incomplete timing telemetry fail verification rather than silently
downgrading the diagnostic artifact.

## CLI and workflow behavior

The credentialed release workflow invokes
`scripts/verify_release_campaign_strict.py`, which cross-validates this artifact
against `comparison.json` before the release-evidence manifest can pass. The
workflow separately records execution, verification, and artifact-upload
outcomes, reports all three, and returns nonzero unless every outcome is
`success`.
