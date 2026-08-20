# Assessment and reporting capability boundary

This document describes the **final** issue #269 topology. The issue #264 migration arrangement, where canonical packages bridged to flat implementation files to preserve path-keyed Pyrefly debt, is no longer an accepted production state.

## Assessment ownership

`firecrawl_skill.research_store.assessment` is the implementation neighborhood for assessment behavior:

- `claims` — claim-manifest persistence;
- `audit` — staged audit service and audit identity;
- `audit_packet` — deterministic audit-packet hashing;
- `coverage` — append-only coverage ledger/service;
- `quality` — extraction-quality assessment;
- `duplicates` and `grouping` — source-independence and evidence grouping;
- `evidence` — EvidencePacket construction/persistence;
- `binding` — semantic claim-to-passage binding;
- `validation` — deterministic EvidencePacket validation and bounded citation-ready output.

The flat `coverage_service.py`, `quality_service.py`, `duplicate_service.py`, `evidence_grouping.py`, `audit_packet.py`, `evidence.py`, `claim_binding_service.py`, and `packet_validator.py` paths are migration artifacts and are forbidden after final cleanup.

## Reporting ownership

`firecrawl_skill.research_store.reporting` is the implementation neighborhood for report behavior:

- `construction` — bounded synthesis, stage persistence/resume, citation-pass orchestration, and deterministic validation-stage integration;
- `validation` — deterministic report/citation/packet-revision validation;
- `artifacts` — PostgreSQL-backed report-validation artifact persistence.

The flat `report_service.py`, `report_validator.py`, and `report_artifact_service.py` paths are migration artifacts and are forbidden after final cleanup.

`ReportValidationSeverity` and EvidencePacket `ValidationSeverity` are typed string enums (`str, Enum`). Serializers emit their string `.value`; callers do not depend on untyped string-constant pseudo-enums.

## Generic service aggregation

`research_store/service.py` has no final role. Corpus behavior belongs to `corpus_service`, claim/audit behavior belongs to `assessment.*`, and export JSON behavior belongs to `export_serialization`. Internal callers must import those owners directly. No replacement miscellaneous aggregation is permitted.

## Persistence-layer exception

`research_store/postgres_audit.py` is intentionally retained. It is **not** a capability compatibility facade: `PostgresAuditRepository` is a connection-bound persistence implementation composed by `postgres_uow_core` alongside `postgres_acquisition`, `postgres_corpus`, `postgres_coverage`, and the other `postgres_*` repositories. Moving it into `assessment` would mix application capability locality with the established repository infrastructure layer and would not remove compatibility scaffolding.

The UoW remains the transaction/composition authority. This cleanup does not change audit SQL semantics, transaction ownership, evidence-reference validation, or PostgreSQL authority.

## Pyrefly migration

The #264 bridge files existed only to avoid silently laundering path-keyed type debt. #269 resolves that staging explicitly:

1. move each implementation to its final owner;
2. repair imports/type diagnostics at the final path;
3. delete the obsolete root implementation/facade;
4. remove baseline records whose source path no longer exists;
5. compare normalized retained debt with the pre-cleanup baseline;
6. never broaden baseline/configuration, add broad suppressions, reduce project scope, or change checker version solely to obtain green status.

## Regression authority

`tests/contract/test_issue_264_assessment_reporting_slice.py` is a final-state structural contract. It asserts physical canonical ownership and absence of the flat capability paths; it no longer proves identity with temporary facades.

Behavioral authority remains cumulative: claims/evidence tests, audit tests, coverage/quality/grouping tests, packet-validation tests, report synthesis/validation/artifact tests, terminal completion-provenance tests, isolated-wheel tests, full Pyrefly, and exact-head CI. PostgreSQL/Qdrant mutation tests must use the repository disposable-service helper.
