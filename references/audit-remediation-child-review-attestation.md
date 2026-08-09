# Audit Remediation child-review attestation — issue #222 / PR #240

This is a post-merge release-gate attestation over the immutable corrective PR
that finally closed issue #222. It exists because the only automated Codex
review recorded before merge was stale relative to the final corrective head.
This document **does not rewrite GitHub history**, convert that stale review into
an approval, or waive the separate-review requirement for the ARC-17 gate PR.

## Immutable scope

| Field | Value |
|---|---|
| Issue | #222 |
| Corrective PR | #240 |
| Base SHA | `173516e1e7f489441da92780bb52107e1f4d134a` |
| Final corrective head | `96c865bc5541a928598e449d7c6f16a0c6c918d0` |
| Merge commit | `91e0d2a46678162807205becbaec34d0247b11d9` |
| Complete diff bytes | 109,819 |
| Diff truncated | no |
| Complete diff SHA-256 | `0770930bbbf9de960e9456aea883cc7dc07752fe7e46bd3bfa8d056c99853b88` |
| Prior automated-review commit | `832d1231707be423bb3cc9a2fbdd77c05c22d1d5` |
| Review freshness | stale relative to final head |

The complete base-to-head diff was re-read through the bounded `gh CLI` plugin;
`bytes_returned == total_bytes == 109819`, `truncated=false`, and the SHA-256
above is the plugin-reported fingerprint.

## Independent final-head revalidation

The immutable final diff was revalidated for the release gate with emphasis on
the defects that forced PR #240:

- run-scoped reconciliation loads exact PostgreSQL membership from a completed
  indexing checkpoint **and its bound persisted asset-membership seal**;
- pre-seal/unverifiable history fails closed rather than reconstructing current
  corpus membership;
- ordinary reconciliation is read-only; payload-index creation and repair occur
  only on explicit write paths;
- run coverage and definition-wide orphans are separate questions;
- payload identity is exhaustively retrieved in bounded 256-ID batches, with an
  explicit 1,376-point regression spanning at least six batches;
- payload indexes use typed Qdrant schemas (`uuid`, `keyword`, `datetime`), read
  from `result.payload_schema`;
- shard health is read from the collection cluster endpoint without a fabricated
  healthy shard fallback;
- alias, vector schema, payload identity, index type, and shard movement failures
  remain blocking discrepancies;
- repair does not silently repoint aliases, destroy/recreate incompatible vector
  collections, or rewrite shard topology; and
- PostgreSQL remains lifecycle, provenance, and exact-membership authority while
  Qdrant remains a rebuildable projection.

The final #240 exact-head CI inventory recorded 32 terminal checks: 30 successful
and two intentionally skipped release-only jobs, with no failed or pending check.
Those skipped post-merge jobs are not treated as if they had run.

## Release-gate disposition

The historical stale-review condition is preserved as an audit fact. ARC-17
resolves its release-risk consequence by (a) retaining this exact immutable
final-head attestation, (b) rerunning the complete #222 reconciliation suite as a
mandatory ARC-17 disposable gate, and (c) requiring a fresh separate formal
review of the **current ARC-17 gate PR head** before merge.
