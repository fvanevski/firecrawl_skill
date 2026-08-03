# RC-10 Aggregate Release-Candidate Gate

Parent epic: #183
Gate issue: #193

## Selected base candidate

The dedicated RC-10 branch was created directly from current `main` after every explicit dependency was merged:

```text
39a157a79098fc6c65fbdb644547ee0a4edc2f49
```

That merge commit has no file-tree difference from the exact RC-9 head `c53c4f637df893b0f2a6de12338e6d12fc08deee`, whose pull-request workflows completed successfully. RC-10 still requires all applicable checks to rerun against the final branch head; prior results are baseline evidence, not a substitute for exact-head validation.

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

RC-10 does not alter the Target A architecture:

- PostgreSQL remains authoritative for workflow, acquisition metadata, provenance, corpus identities, evidence, audits, and durable jobs.
- `BLOB_ROOT` remains the immutable content-addressed store for provider payload bytes referenced by PostgreSQL.
- Qdrant remains a rebuildable projection that is activated only after complete processing and reconciliation.
- Valkey remains optional transient coordination; durable recovery proceeds from PostgreSQL.
- Supported acquisition must complete authoritative preflight before provider construction or network execution and must return stable identifiers only after persistence commits.
- Presentation exports and bounded ephemeral files are never runtime authority.

Payload bytes are not moved into PostgreSQL by this release candidate.

## Exact-head evidence requirements

The final branch head must pass the repository pull-request workflows without weakening tests:

1. `CI`, including Ruff, release invariants, disposable-PostgreSQL/Qdrant tests, and strict campaign contracts on Python 3.11 and 3.12.
2. `Acquisition authority contract`, including failure-before-provider ordering.
3. `Authoritative fsearch contract` and `Authoritative fscrape contract`.
4. `Research environment adapter`.
5. `Authoritative storage gates`, including disposable PostgreSQL, Qdrant, Valkey, worker/recovery, blob-integrity, deterministic export, documentation/parser, source-policy, and monitored-temporary-directory contracts on Python 3.11 and 3.12.

The exact final head, workflow conclusions, test counts, skipped external validation, and residual risks belong in issue #193 and the draft pull request. They are intentionally not embedded here because changing this file would itself change the candidate SHA.

## Compatibility review boundary

The release notes, migration guide, recovery drill checklist, authoritative workflows, and research-store architecture remain the controlling operator documents. RC-10 introduces no schema migration, data migration, payload relocation, command-surface change, Qdrant redesign, or Valkey correctness dependency.

## Non-goals

- No runtime implementation changes.
- No adjacent cleanup or formatting churn.
- No new persistence path or compatibility bridge.
- No weakening or replacement of the existing exact-head gates.
