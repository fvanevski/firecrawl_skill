# RC-10 Aggregate Release-Candidate Gate

Parent epic: #183
Gate issue: #193

## Selected base candidate

The dedicated RC-10 branch was created directly from current `main` after every explicit dependency was merged:

```text
39a157a79098fc6c65fbdb644547ee0a4edc2f49
```

That merge commit has no file-tree difference from the exact RC-9 head `c53c4f637df893b0f2a6de12338e6d12fc08deee`, whose pull-request workflows completed successfully. Prior results are baseline evidence, not a substitute for exact-head validation.

## Dependency closure

| Issue | Merged pull request | Merge commit |
|---|---:|---|
| #184 | #194 | `c80dc13d3619232d663d3e4a458dc11eefa5bf81` |
| #185 | #195 | `f6b9df35ba7d91e84309d47414d27a4658d53ee1` |
| #186 | #196 | `1db5643519a4e6307ed376c27db22b4efb5e80b3` |
| #187 | #197 | `a8c7b81e65c79d77725f8209990d4d75dd89c799` |
| #188 | #198 | `82d3369c0be9bba381f38b598c3b05ed4b683ae6` |
| #189 | #199 | `1aaa92f7c3a84ea1ed210947130b120cc814826e` |
| #190 | #200 | `f2b32ff26f1f7735fa07f1aac3134ff3d7d061e5` |
| #191 | #201 | `8317bccf2a2b339996bab8a7aa890ba0c51e2928` |
| #192 | #202 | `39a157a79098fc6c65fbdb644547ee0a4edc2f49` |

## Authority contract under review

RC-10 preserves the Target A architecture:

- PostgreSQL remains authoritative for workflow, acquisition metadata, provenance, corpus identities, evidence, audits, and durable jobs.
- `BLOB_ROOT` remains the immutable content-addressed store for provider payload bytes referenced by PostgreSQL.
- Qdrant remains a rebuildable projection that is activated only after complete processing and reconciliation.
- Valkey remains optional transient coordination; durable recovery proceeds from PostgreSQL.
- Supported acquisition must complete authoritative preflight before provider construction or network execution and must return stable identifiers only after persistence commits.
- Presentation exports and bounded ephemeral files are never runtime authority.

Payload bytes are not moved into PostgreSQL by this release candidate.

## Pre-merge exact-head requirements

Every update to the RC branch must pass all applicable pull-request workflows on the exact branch head without weakening tests:

1. `CI`, including Ruff, release invariants, disposable-PostgreSQL/Qdrant tests, strict campaign contracts, and classified skip evidence on Python 3.11 and 3.12.
2. `Acquisition authority contract`, including failure-before-provider ordering.
3. `Authoritative fsearch contract` and `Authoritative fscrape contract`.
4. `Research environment adapter`.
5. `Authoritative storage gates`, including disposable PostgreSQL, Qdrant, Valkey, worker/recovery, blob-integrity, deterministic export, documentation/parser, source-policy, and monitored-temporary-directory contracts on Python 3.11 and 3.12.

The draft RC pull request must use `Refs #193`, not `Closes #193`. Merging the pull request does not itself satisfy or close issue #193 because the credentialed release campaign can validate only the resulting exact current `main` SHA.

## Post-merge release boundary

After the corrective RC pull request merges:

1. Resolve the exact resulting `main` SHA; do not infer it from the pull-request head or proposed merge commit.
2. Confirm all push-triggered exact-head gates pass on that SHA.
3. Dispatch the `Real release campaign` workflow from `refs/heads/main` with `candidate-sha` equal to that same SHA.
4. Require the campaign identity guard to prove that the candidate SHA, dispatch SHA, workflow SHA, and checked-out repository SHA are identical.
5. Require successful campaign execution, authoritative evidence verification, and artifact upload.
6. Record the workflow run ID, artifact ID, artifact digest, exact candidate SHA, skipped or unavailable external checks, and residual risks in issue #193.
7. Close issue #193 and parent epic #183 only after the credentialed campaign succeeds. A campaign failure requires an issue-scoped corrective PR and a new exact-head campaign; it must not be waived as residual risk.

The required artifact is named `real-release-campaign-<exact-main-sha>` and must remain bound to the verified exact candidate through the campaign contract.

## Compatibility review boundary

The release notes, migration guide, recovery drill checklist, authoritative workflows, and research-store architecture remain the controlling operator documents. RC-10 introduces no schema migration, data migration, payload relocation, command-surface change, Qdrant redesign, or Valkey correctness dependency.

## Corrective gate-fix scope

RC-10 may contain only defects discovered while validating the aggregate candidate, together with their regression tests and gate/documentation corrections. It must not contain adjacent cleanup, unrelated formatting churn, a new persistence path, a compatibility bridge, or a weakening or replacement of existing exact-head gates.
