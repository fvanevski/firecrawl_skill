# Doctor diagnostics contract

## Scope

`research-db doctor` is the operator-facing diagnostic surface for issue #220 / RC-17. The canonical structured output schema is `doctor-diagnostics-v1`. The thin `scripts/research-db` launcher routes `doctor` to the typed `research_store.doctor_command` entry point; other `research-db` commands continue to use the existing CLI module.

This is a CLI/output compatibility change only. It does not change the PostgreSQL schema and requires no Alembic migration.

## Authority boundary

Doctor is observational. PostgreSQL remains authoritative for workflow, corpus identity, durable jobs, and exact membership. `BLOB_ROOT` remains the immutable content-addressed payload store referenced by PostgreSQL. Qdrant is only a rebuildable projection and may never establish lifecycle, provenance, or exact-membership truth. Valkey is transient coordination.

Doctor never fabricates historical provenance, repairs lifecycle state, deletes orphan blobs, or treats a provisional/external synthesis result as authoritative completion evidence.

## Seven independent domains

Every diagnostic report contains these seven top-level domains:

| Domain | Meaning |
|---|---|
| `postgres_authority` | PostgreSQL connectivity and authoritative schema availability |
| `referenced_blob_integrity` | Integrity of immutable blobs referenced by persisted PostgreSQL snapshots |
| `unreferenced_blob_inventory` | Global blob files that are not referenced by persisted snapshots |
| `index_job_health` | PostgreSQL manifest/job reconciliation health |
| `qdrant_projection` | Compatibility and coverage of the rebuildable Qdrant projection |
| `worker_health` | Durable worker heartbeat, lease, dead-job, and stale-lease health |
| `environment_connectivity` | Valkey, embedding, and reranker endpoint connectivity |

Each domain has one `status` from:

- `pass`: the domain was checked and its required conditions passed;
- `warning`: the domain has operator-visible inventory or degradation that does not invalidate another authoritative domain;
- `failure`: the domain was checked and a required condition failed;
- `inconclusive`: the domain cannot be interpreted authoritatively because a prerequisite is absent or unavailable.

A nonempty `unreferenced_blob_inventory` is a `warning`; it does **not** change an otherwise healthy `referenced_blob_integrity` result and does not by itself make doctor exit nonzero. Missing or corrupt referenced blobs remain a `failure`.

## Qdrant projection semantics

`qdrant_projection` compares the configured active alias with the PostgreSQL index definition and current PostgreSQL chunk IDs. Embedding-fingerprint mismatch, incompatible schema, or point-membership mismatch is a failure. Exact point coverage cannot overwrite an earlier compatibility failure.

An absent active projection, or a projection inspected while PostgreSQL authority is unavailable, is `inconclusive`; it is never promoted to authoritative corpus truth.

## Connectivity failure classes

Failures expose a stable machine-readable `reason_code`, bounded/redacted `detail`, and actionable `remediation` where applicable.

| Reason code | Meaning | Typical remediation |
|---|---|---|
| `network_policy_denial` | Host/container/sandbox policy rejected the socket or operation | Permit the required operation in the active policy boundary |
| `server_unavailable` | Service is not accepting connections or the connection timed out | Verify address/port and service liveness |
| `credential_failure` | Authentication, authorization configuration, or required endpoint configuration failed | Verify credentials/configuration without printing secrets |
| `network_namespace_denial` | Routing, DNS, or namespace reachability failed | Verify namespace attachment, routes, and DNS |
| `database_rejection` | PostgreSQL rejected an otherwise reachable operation, including role/table privilege failures | Correct role privileges, schema, or rejected query |
| `query_runtime_failure` | The operation failed for a non-connectivity runtime/query reason | Inspect the named component and its logs |

Classification uses exception type/errno plus component context before textual fallbacks. Compact `errno111` and lower-cased `connect econnrefused` are treated as server-unavailable forms; PostgreSQL `permission denied for table ...` is a database rejection, not a network-policy denial.

## Secret handling

Diagnostic detail is not an authentication-data sink. Before inclusion in doctor output, common bearer-token, password, API-key/token/secret assignment, and URL-userinfo forms are replaced with `[REDACTED]`; detail is bounded to 1000 characters. Tests use sentinel secrets and assert that they do not appear in output.

This is defense in depth. Operators must still avoid placing secrets in arbitrary exception text and must not rely on doctor output as a secure secret store.

## Human and JSON output

JSON remains the default:

```bash
scripts/research-db doctor
```

Human-readable output preserves the same seven independent status conclusions and reason codes:

```bash
scripts/research-db doctor --human
```

The human renderer does not recompute or collapse status; it renders the same structured report.

## Reset and compatibility consumers

`scripts/reset-firecrawl-research` is synchronized with `doctor-diagnostics-v1`. Its final clean-state gate requires all seven relevant clean-state domains and nested environment components rather than the removed `.blobs`, `.qdrant`, `.index_reconcile`, `.valkey`, and endpoint `.ok` fields.

External automation consuming the pre-#220 doctor JSON shape must migrate to `doctor-diagnostics-v1` keys. The schema version exists specifically so consumers can fail closed rather than guess which shape they received.

## Regression gates

`tests/unit/test_issue_220_doctor_diagnostics.py` exercises the production diagnostic service and canonical launcher contract. It covers:

- all seven domains and per-domain status;
- healthy referenced blobs plus unrelated orphan inventory;
- JSON/human category parity;
- all six connectivity reason-code classes;
- `connect ECONNREFUSED` and compact `errno111` ordering;
- PostgreSQL table-permission rejection vs sandbox policy denial;
- Qdrant compatibility failure with exact point coverage;
- credential redaction;
- reset-script selector synchronization.

The dedicated `Audit regression baseline` workflow runs this suite on Python 3.11 and 3.12 together with the frozen audit regressions and skip/xfail allowlist verifier.
