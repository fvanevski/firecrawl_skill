# Audit Remediation RC — ARC-17 release-gate contract

Issue: #223
Epic: #206
Branch: `rc/audit-17-release-gates`

This document is the human-readable companion to
`references/audit-remediation-release-gates.json`. The JSON file is the
machine-readable source for gate IDs, exact commands, expected evidence,
execution phases, and artifacts. Source-controlled gate rows always say
`source_result=pending`; only immutable execution evidence may report pass or
fail.

## Release rule

**No blocking gate may be waived.** A failed gate requires a corrective commit
or reopening the responsible child issue. Evidence from an earlier PR head,
workflow attempt, merge target, default-branch commit, or release target is
stale for a later SHA.

The gate PR deliberately uses **`Refs #223`**, not `Closes #223`. Issue #223 and
epic #206 **must remain open** after the gate PR merges. They close only after
the exact resulting `main` commit has completed the post-merge release-evidence
workflow, the disposable ARC-17 matrix, the credentialed **Real release
campaign**, release creation, and all required readbacks.

## Evidence phases

### 1. Pre-merge exact-head CI

Every gate-PR head must independently pass:

- unit/release-invariant tests on Python 3.11 and 3.12;
- the repository broad suites on Python 3.11 and 3.12 with classified skips;
- strict-campaign contract tests;
- Ruff lint and formatting;
- the release/gate **type gate**;
- the complete disposable ARC-17 gate job against PostgreSQL 16 and Qdrant;
- all dedicated authority, fsearch, fscrape, extraction, checkpoint,
  reconciliation, storage, and audit-regression workflows.

The disposable gate job checks out the exact PR head SHA rather than GitHub's
synthetic merge ref, verifies a clean tree, records the Git tree hash and service
versions, executes every `execution_phase=disposable` matrix row, stores stdout
and stderr separately, and records byte counts and SHA-256 digests. Its complete
evidence directory is uploaded for **90 days**.

A green PR is necessary but not release evidence. In particular, pull-request
CI intentionally does not run the final credentialed release campaign.

### 2. Post-merge exact-main evidence

After an explicitly authorized exact-head merge:

1. Resolve the actual resulting `main` SHA from GitHub; do not infer it from the
   PR head or a proposed merge commit.
2. Require the push-triggered CI and ARC-17 disposable matrix to pass on that
   exact `main` SHA.
3. Generate the exact-head release-evidence manifest and retain its artifact ID,
   digest, run ID, attempt, and candidate SHA.
4. Dispatch `.github/workflows/release-campaign.yml` from `refs/heads/main` with
   `candidate-sha` equal to the exact current `main` SHA.
5. Require identity equality among candidate SHA, dispatch SHA, workflow SHA,
   and checked-out `HEAD`.

### 3. Credentialed Real release campaign

The self-hosted release runner executes the existing two strict campaigns,
strict PostgreSQL-bound verification, and the release contract. Before the
campaign directory is uploaded, `scripts/scan_release_secrets.py` scans it for
all configured runtime secret values and high-confidence credential material.
The scanner reports only file/reason metadata; it never serializes a secret
value.

The campaign artifact remains named `real-release-campaign-<exact-main-sha>`
and is retained for 90 days. A secret-scan failure, strict-verifier failure,
execution failure, or upload failure blocks release.

## Migration and rollback semantics

The research-store migrations are forward-only. ARC-17 therefore treats
"rollback rehearsal" as a restore drill, not as an Alembic downgrade:

1. migrate a disposable database to the supported previous revision;
2. persist a sentinel proving pre-upgrade authority;
3. create a PostgreSQL backup;
4. migrate forward to current head;
5. restore the backup into a recreated database;
6. verify the restored revision and sentinel; and
7. migrate the restored database forward to head again.

`scripts/audit_migration_rehearsal.py` performs the fresh-database, forward, and
restore/recovery portions against disposable PostgreSQL. A migration that
implements `downgrade()` as fail-closed remains correct; ARC-17 does not weaken
that policy merely to make a rollback checkbox green.

## Exact audited-race evidence

The release matrix separately proves:

```text
expected      = 1376
complete      = 1344
claimable     = 0
running_live  = 32
```

A zero-claim observation with 32 live jobs is explicitly nonterminal. The run
must remain in indexing, reobserve the exact PostgreSQL census, and advance only
after the complete class reaches 1,376 with every non-complete class at zero.
Restart/resume and concurrent finish/resume gates are separate rows so one
neighboring test cannot stand in for another invariant.

## Secret-scan boundary

Two scans are required:

- the disposable gate scans repository fixtures plus generated gate logs and
  exports for high-confidence credential material;
- the credentialed release campaign additionally supplies the names of the
  actual release secret environment variables so the scanner can detect an
  exact runtime-secret escape without writing the secret into its report.

Oversized files are not silently ignored: an unscanned file makes the secret
scan fail closed.

## Child-review provenance

Issue #222 required corrective PR #240 after PR #239 merged before independent
review completed. The immutable final #240 diff is separately recorded in
`references/audit-remediation-child-review-attestation.md`. That record does not
pretend the earlier stale review was current and does not rewrite GitHub history.
It provides an explicit release-gate attestation over the actual corrective head
and complete diff fingerprint.

## Final release closure

Only after all phases pass may the maintainer authorize creation of the RC
release at the exact verified release commit. Release notes, migration notes,
compatibility notes, rollback guidance, known limitations, artifact identities,
and hashes must refer to that same release SHA. Issue #223 and epic #206 close
only after release creation and readback confirm the tag, release URL, and target
commit.
