# Assessment and reporting boundary — issue #264

Issue #264 is a structural-refactor slice. It changes responsibility locality,
not persistence authority or evidence/report semantics.

## Before

Assessment responsibilities were flat peers under `research_store`:

- `coverage_service.py` — append-only coverage ledger/service;
- `service.py` — unrelated corpus re-exports plus claim manifest persistence and
  staged audit persistence;
- `evidence.py` and `claim_binding_service.py` — EvidencePacket construction and
  semantic claim binding;
- `quality_service.py`, `duplicate_service.py`, `evidence_grouping.py` — quality,
  source independence and evidence grouping;
- `packet_validator.py` — packet completeness and referential validation;
- `audit_packet.py` — deterministic authoritative audit-packet identity.

Reporting responsibilities were also flat peers:

- `report_service.py` — staged report construction and citation pass;
- `report_validator.py` — deterministic exact-ID/revision/citation validation;
- `report_artifact_service.py` — PostgreSQL-backed validation-stage persistence.

The generic `service.py` therefore had no single capability boundary.

## After

`research_store.assessment` is the canonical assessment neighborhood:

- `assessment.claims` owns `ClaimManifestService`;
- `assessment.audit` owns `AuditService` and audit identity functions;
- `assessment.audit_packet` owns deterministic PostgreSQL audit-packet hashing;
- `assessment.coverage` owns the append-only coverage ledger/service;
- `assessment.quality` owns extraction-quality assessment;
- `assessment.duplicates` and `assessment.grouping` own deterministic source-
  independence and evidence-grouping logic;
- `assessment.evidence`, `.binding`, and `.validation` provide the canonical
  namespace for the baseline-tracked EvidencePacket implementation surfaces.

`research_store.reporting` is the canonical report neighborhood:

- `reporting.construction` — report construction/citation pass;
- `reporting.validation` — deterministic report, claim, citation and packet-
  revision validation;
- `reporting.artifacts` — authoritative report-validation artifact persistence.

`research_store.service` is now only an identity-preserving compatibility facade
for legacy imports plus the historical serialization helpers. It owns no claim
or audit implementation. The old debt-free flat capability paths are likewise
identity-preserving compatibility facades pointing into the canonical packages.

## Staged compatibility and Pyrefly baseline

This repository's `pyrefly-baseline.json` is path-keyed pre-existing debt. The
following implementation files have reviewed baseline diagnostics and are not
physically relocated in #264:

- `evidence.py`;
- `claim_binding_service.py`;
- `packet_validator.py`;
- `report_service.py`;
- `report_validator.py`;
- `postgres_audit.py`.

Relocating those files would make diagnostics disappear from the baseline by
path rather than fixing them. Canonical assessment/reporting modules therefore
bridge to those exact implementations. Issue #269 owns final compatibility
cleanup after debt is addressed explicitly.

Debt-free `coverage_service.py`, `quality_service.py`, `duplicate_service.py`,
`evidence_grouping.py`, `audit_packet.py`, and `report_artifact_service.py` were
moved in the opposite direction: implementation ownership now lives under the
canonical capability packages and the old flat paths are temporary facades.

## Preserved invariants

Issue #264 does **not** change:

1. PostgreSQL authority for coverage, claims/evidence links, EvidencePackets,
   audit records, synthesis stages or report-validation artifacts.
2. Claim, candidate, snapshot, passage, packet-revision or citation identities.
3. EvidencePacket exact-reference and source-version validation.
4. Audit identity hashes, model fingerprints, stage persistence/reuse/staleness
   semantics, audit-packet hashing, or evidence-reference validation.
5. Report hash, exact EvidencePacket revision binding, claim manifests or
   citation-validation behavior.
6. Terminal completion provenance. `completion_provenance.py` remains the
   read/lock authority used by the lifecycle guard; success still requires the
   same PostgreSQL-authoritative provenance chain.
7. Existing public imports. Legacy imports resolve to the same class/function
   objects while the campaign compatibility facades remain.

## Regression evidence

The issue-specific structural tests prove canonical ownership, legacy identity,
wheel packaging, and compatibility-bridge identity. Existing claims/evidence,
audit, coverage, quality, packet-validator, report-validator, workflow/terminal,
and integration tests remain the behavioral authorities. The exact-head review
workflow runs changed-scope Ruff and Pyrefly, full-project Pyrefly, focused
pytest, and `git diff --check` against the immutable PR base/head pair.
