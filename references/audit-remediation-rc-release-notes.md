# Audit Remediation RC — pre-release notes

Issue: #223
Epic: #206

These notes are deliberately **pre-release**. **No RC tag exists yet**, no release
URL is claimed, and no release commit is named until the gate PR is merged and
the exact resulting `main` SHA completes all post-merge evidence.

## Scope

The Audit Remediation RC aggregates issues #207–#222 and the ARC-17 gate work in
#223. It preserves the established Target A authority model:

- PostgreSQL is authoritative for lifecycle, provenance, exact membership,
  durable jobs, completion, and synthesis provenance;
- `BLOB_ROOT` retains immutable content-addressed provider/evidence bytes;
- Qdrant remains a rebuildable projection; and
- Valkey is transient coordination rather than durable correctness authority.

## Pre-merge evidence

The current gate PR must publish exact-head CI evidence for unit, formatting,
lint, type, broad integration, and every disposable ARC-17 matrix row. A PR-head
artifact is review evidence only. It is stale after any new commit and is not a
release artifact.

## Post-merge evidence

After an explicitly authorized merge, the release candidate is the actual
resulting `main` commit. Push-triggered CI, disposable gate evidence, and the
exact-head release-evidence manifest must all bind to that commit. The Real
release campaign then runs against the same SHA and produces the credentialed
campaign artifact only after strict verification and secret scanning.

## Migration notes

The dependency chain introduced forward-only PostgreSQL migrations through
revision 0044. ARC-17 itself does not add a schema migration. Release validation
covers both an empty-database migration and a supported-previous-revision forward
upgrade.

Historical provenance remains conservative: migrations do not synthesize
missing promotion, provider-attempt, lifecycle, or completion history merely to
make a release gate pass.

## Rollback and recovery

Database downgrade remains intentionally unsupported. Operational database
rollback is **forward repair or PostgreSQL backup restoration**. ARC-17 rehearses
restore from the supported previous schema boundary and verifies that the
restored database can migrate forward again.

Projection rollback remains separate: Qdrant can be rebuilt from PostgreSQL and
alias changes continue through explicit index activation/rollback operations.
Qdrant state never authorizes a lifecycle or completion transition.

## Compatibility

The gate changes add release-validation tooling and documentation; they do not
change the public research CLI/JSON authority contracts. Release evidence is
additional presentation/audit material and is never replay, retry, selection,
ingestion, lifecycle, or completion authority.

## Known limitations before release

Until post-merge execution finishes, the following are intentionally unresolved
and must not be described as passed:

- the credentialed Real release campaign;
- final release-asset secret scan with the actual configured runtime secrets;
- final `main` release-evidence artifact identities/digests;
- release tag, release URL, and target-commit readback; and
- closure of issue #223 and epic #206.

A failure in any item above requires correction and a new exact-head run; it is
not a nonblocking waiver.
