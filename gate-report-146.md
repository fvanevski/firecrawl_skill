# Gate #146 — Release Revalidation Gate Report

**Gate issue:** [#146](https://github.com/fvanevski/firecrawl_skill/issues/146)
**Gate date:** 2026-07-28
**Candidate SHA:** `2fca41d5379d8a8262ab90288eafbe60998f2fff`
**Tree hash:** `9923bd78b49987b72f64f2e3c3210f8e11b44662`
**Branch:** `main`
**Decision:** **FAIL — release remains blocked**

---

## 1. Corrective issues and PRs

| Issue | Title | State | PR | Merge SHA |
| ------- | ------- | ------- | ----- | ----------- |
| #142 | Replace heuristic benchmark quality scores with authoritative measurements | CLOSED | #147 | `3287743` |
| #143 | Add run-scoped measured benchmark performance telemetry | CLOSED | #148 | `6327632` |
| #144 | Execute and publish two strict real release benchmark campaigns | CLOSED | #149 | `81d7360` |
| #145 | Enforce exact release-candidate CI and immutable release provenance | CLOSED | #150 | `2fca41d` |

All four blockers are closed as completed. All four PRs were reviewed (Codex automated review, all COMMENTED) and merged. All required CI checks passed on each PR.

---

## 2. Release candidate

| Field | Value |
| ------- | ------- |
| SHA | `2fca41d5379d8a8262ab90288eafbe60998f2fff` |
| Tree hash | `9923bd78b49987b72f64f2e3c3210f8e11b44662` |
| Tag/version | Not yet assigned (empty) |
| Candidate commit | `[P7-R04 / #145] Exact candidate CI and provenance (#150)` |
| Post-candidate commits | 0 (HEAD == candidate) |
| CI workflow run on candidate | `30416824406` (push-to-main, conclusion: **failure**) |

---

## 3. Quality metrics

### Definitions and versions

| Metric | Version | Source | Formula |
| -------- | --------- | -------- | --------- |
| Candidate recall | `quality-metrics-v1` | `tests/fixtures/benchmark/ground_truth.json` (gt-v1) | `retrieved_relevant / labeled_relevant_set` |
| Source quality | `quality-metrics-v1` | Benchmark annotations + required source classes | Versioned annotation-based |
| Coverage | `quality-metrics-v1` | Coverage ledger (coverage-ledger-v1.json) | `satisfied_applicable / total_applicable` |
| Unsupported-claim rate | `quality-metrics-v1` | Final claim manifest + report validator | `unsupported_claims / total_claims` |
| Citation accuracy | `quality-metrics-v1` | Exact citation-to-passage validation | `valid_citations / total_citations` |
| Report quality | `quality-metrics-v1` | Versioned rubric (coverage: 0.30, citation: 0.30, support: 0.25, packet: 0.15) | Weighted composite |

### Heuristic audit

**No heuristics found in the new telemetry path.** The old heuristic `semantic_calls * 500` is confined to the legacy fallback in `release_benchmark.py:708` (guarded by `if not telemetry_tables`). The regression test `test_performance_telemetry.py:745` confirms `"semantic_calls * 500"` does not appear in `telemetry_service.py`.

### Test coverage

- `scripts/test_release_benchmark.py` — comprehensive metric engine tests
- `scripts/test_release_evidence.py` — manifest generation, verification, mismatch detection
- `scripts/test_strict_campaign.py` — strict-mode enforcement, campaign comparison
- All unit tests pass (16 passed, 2 skipped in `test_strict_campaign.py`)

### Gap: No real campaign execution

The quality metrics code is correct and well-tested, but **no real campaigns have been executed**. The recovery report (`recovery-report.txt`) contains test placeholder IDs (`fr_bench_test`), not real run IDs. The campaign artifact directory `/tmp/firecrawl_strict_campaign/` is empty. No `result.json`, `environment.json`, `comparison.json`, or `manifest.json` files exist from real execution.

---

## 4. Performance telemetry

### Schema and versions

| Schema | Version | Table |
| -------- | --------- | ------- |
| `PerformanceMeasurement` | `performance-measurement-v1` | `run_performance_telemetry` |
| `TokenAccounting` | `token-accounting-v1` | `endpoint_usage_records` |
| `ResourceSample` | `resource-sample-v1` | `run_resource_samples` |
| `EndpointUsageRecord` | `endpoint-usage-v1` | `endpoint_usage_records` |
| Migration | `0036_run_performance_telemetry.py` | 5 new tables |

### Measurement provenance

| Metric | Source | Formula |
| -------- | -------- | --------- |
| Tokens | `endpoint_usage_records` (endpoint-first) / `tiktoken` (tokenizer-fallback) | Actual endpoint `usage` field or tokenizer count |
| Cache events | `run_cache_events` (run-scoped, stage-scoped) | `hits / lookups` |
| Embedding throughput | `run_embedding_throughput` | `total_texts / elapsed_seconds` |
| Latency | `research_runs` timestamps | `wall_clock_ms(monotonic_start, monotonic_end)` |
| CPU/GPU | `run_resource_samples` (multi-sample, configurable interval) | Mean/Max across run window |

### Availability tracking

- `token_source = "unavailable"` when neither endpoint nor tokenizer provides counts
- CPU/GPU samples carry explicit `status` field: `"measured"`, `"unavailable"`, `"partial"`, `"stale"`, `"invalid"`
- Unavailable metrics are `None` or `"unavailable"`, never `0.0`
- Strict mode raises `RuntimeError` when required telemetry is unavailable

### Gap: No real campaign execution

Telemetry is correctly implemented but **no real campaigns have been executed** to produce actual telemetry data.

---

## 5. Campaign A and B

### Status: NOT EXECUTED

| Check | Result |
| ------- | -------- |
| Real Campaign A execution | **NOT EXECUTED** — no real run IDs |
| Real Campaign B execution | **NOT EXECUTED** — no real run IDs |
| Campaign artifact directory | Empty (`/tmp/firecrawl_strict_campaign/` — 0 bytes) |
| Persisted PostgreSQL state | None (no disposable test database running) |
| Recovery report IDs | Test placeholders (`fr_bench_test`), not real UUIDs |
| Simulation quality constants | Explicitly marked `PLACEHOLDER: UNVERIFIED` |
| Reproducibility comparison | Not executed against real data |

### Code paths exist

- Three operationally distinct modes defined: `agent_led`, `autonomous_local`, `deterministic_debug`
- Legacy mode correctly forbidden
- Integrity checks defined in `strict_benchmark.py`
- Reproducibility comparison logic implemented

### Gap: No real execution

The gate requires "two complete real executions" with "identical versioned inputs and documented environmental controls." Neither Campaign A nor Campaign B has been executed against real infrastructure. The recovery report's PASS claim is a test artifact, not evidence of real reproducibility.

---

## 6. Exact-head CI and provenance

### Candidate SHA: `2fca41d5379d8a8262ab90288eafbe60998f2fff`

### CI run on candidate SHA (push-to-main, run ID 30416824406)

| Job | Conclusion |
| ----- | ----------- |
| Test — Python 3.11 | **SUCCESS** |
| Test — Python 3.12 | **SUCCESS** |
| Ruff | **SUCCESS** |
| Strict Campaign (issue #144) — Python 3.11 | **SKIPPED** |
| Strict Campaign (issue #144) — Python 3.12 | **SKIPPED** |
| Release evidence (issue #145) | **FAILED** |

### CI run on PR (PR #150)

| Job | Conclusion |
| ----- | ----------- |
| Test — Python 3.11 | SUCCESS |
| Test — Python 3.12 | SUCCESS |
| Ruff | SUCCESS |
| Strict Campaign — Python 3.11 | SUCCESS |
| Strict Campaign — Python 3.12 | SUCCESS |

### Discrepancy

The Strict Campaign checks passed on the PR but were **SKIPPED** on the push-to-main run. This is likely a GitHub Actions issue where matrix jobs with `if` conditions are sometimes skipped on push events. The `if` condition `github.event_name == 'push' || github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'` should evaluate to true for push events.

The Release evidence check **FAILED** on the push-to-main run. The `verify` step reported "One or more artifact hashes are missing or invalid." The `recovery-report.txt` file is untracked (`??`) and was modified after the manifest was generated, causing a hash mismatch.

### Provenance

- **No post-candidate commits** exist on `main`.
- **No release tag** has been assigned yet.
- **CI workflow run IDs** are recorded (PR: multiple, push-to-main: 30416824406).
- **Fingerprints** present: Python, platform, dependencies (alembic, psutil, psycopg, PyYAML, qdrant-client, redis, SQLAlchemy), service (postgres:16-alpine), model (nomic-embed-text-v1.5), tokenizer (tiktoken==0.7.0), dataset (benchmark-release-v1), ground_truth (gt-v1), hardware (x86_64).

---

## 7. Required suites and evidence

| Suite | Status |
| ------- | -------- |
| Ruff lint and formatting | ✅ Passed (PR and push-to-main) |
| Python 3.11 and 3.12 tests | ✅ Passed (PR and push-to-main) |
| Migrations | ✅ Included in test suite |
| PostgreSQL integration | ✅ Included in test suite (PR) |
| Qdrant integration and recovery | ✅ Included in test suite (PR) |
| Extraction and EvidencePacket | ✅ Included in test suite (PR) |
| Retrieval and EvidencePacket | ✅ Included in test suite (PR) |
| Synthesis and report validation | ✅ Included in test suite (PR) |
| Cache and resource governance | ✅ Included in test suite (PR) |
| Quality metric integration | ✅ Included in test suite (PR) |
| Performance telemetry integration | ✅ Included in test suite (PR) |
| Strict campaign A | ❌ **SKIPPED** on push-to-main |
| Strict campaign B | ❌ **SKIPPED** on push-to-main |
| Reproducibility comparison | ❌ Not executed (no real campaigns) |
| Index recovery drill | ✅ Included in test suite (PR) |
| Evidence-manifest verification | ❌ **FAILED** on push-to-main |

---

## 8. Open defects

### P0 (release-blocking)

| # | Description |
|---|-------------|
| 1 | **[P0 / #151] Strict Campaign checks SKIPPED on push-to-main** — The CI workflow's Strict Campaign job is not executing on push events despite the `if` condition. This is a workflow bug that must be fixed before release. |
| 2 | **[P0 / #152] Release evidence check FAILED on push-to-main** — The `verify` step reports artifact hash mismatch (untracked `recovery-report.txt`). See issue #152. |
| 3 | **[P0 / #153] Campaign A and B never executed against real infrastructure** — The gate requires two complete real executions. No real run IDs, no real artifacts, no real telemetry data exist. See issue #153. |

### P1

| # | Description |
|---|-------------|
| 1 | No release tag/version assigned yet. |
| 2 | Recovery report contains test placeholder IDs, not real campaign IDs. |
| 3 | Simulation quality constants marked `PLACEHOLDER: UNVERIFIED`. |

### P2

| # | Description |
|---|-------------|
| 1 | The `recovery-report.txt` is untracked and can cause manifest hash mismatch. |
| 2 | The Strict Campaign `if` condition may need simplification to avoid GitHub Actions matrix/skip bugs. |

---

## 9. Decision

### **FAIL — release remains blocked**

### Reasons

1. **CI did not pass on the exact candidate SHA.** The push-to-main workflow run (ID 30416824406) on `2fca41d` had two SKIPPED jobs (Strict Campaign) and one FAILED job (Release evidence). The gate requires "complete required CI ran against that exact SHA" and "all required jobs passed."

2. **Campaign A and B were never executed against real infrastructure.** The gate requires "two complete real executions" with "identical versioned inputs." No real run IDs, no real artifacts, no real telemetry data exist. The recovery report's PASS claim is a test artifact.

3. **Quality metrics and performance telemetry are correctly implemented** but have never been validated against real campaign data. The code is sound; the evidence is missing.

### Required remediation

1. Fix the Strict Campaign CI `if` condition so it executes on push-to-main.
2. Fix the Release evidence verification (artifact hash mismatch).
3. Execute Campaign A and B against real infrastructure with real PostgreSQL, Qdrant, and endpoint connections.
4. Re-run CI on the candidate SHA (or a new candidate SHA) and verify all jobs pass.
5. Assign a release tag/version after the candidate passes all checks.
