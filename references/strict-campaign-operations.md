# Strict Campaign Operations Guide

Operations guide for executing strict release benchmark campaigns (issue #144).

## Table of contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Command-line reference](#3-command-line-reference)
4. [Execution flow](#4-execution-flow)
5. [Artifact structure](#5-artifact-structure)
6. [Reproducibility comparison](#6-reproducibility-comparison)
7. [Recommendation logic](#7-recommendation-logic)
8. [Failure modes and recovery](#8-failure-modes-and-recovery)
9. [CI integration](#9-ci-integration)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

The strict campaign CLI (`strict_benchmark`) executes two complete benchmark
campaigns (A and B) with identical versioned inputs, compares them for
reproducibility, and produces a release recommendation.

**Strict mode is mandatory and cannot be disabled.** There is no `--no-strict`
or `--simulate` flag. When strict mode is active:

- Missing, partial, estimated, or stale quality or performance metrics produce
  `0.0` values with `unavailable` status and clear formulas documenting the
  empty source, rather than falling back to heuristics.
- Failed deterministic integrity checks force a `NO_GO` recommendation.
- A campaign that recommends `NO_GO` causes the CLI to exit with status `1`.
- Reproducibility differences outside the configured tolerance cause the CLI to
  exit with status `1`.

### Supported modes

| Mode                | Description                                      |
| ------------------- | ------------------------------------------------ |
| `agent_led`         | LLM-planned multi-branch research with handoffs  |
| `autonomous_local`  | Single-pass local execution without planning      |
| `deterministic_debug` | Deterministic execution with debug-level output |

These three modes are operationally distinct and architecturally independent.
The `legacy` mode is permanently forbidden — requesting it raises a
`RuntimeError`.

---

## 2. Prerequisites

| Requirement                       | Details                                           |
| --------------------------------- | ------------------------------------------------- |
| Python 3.11 or 3.12              | Tested on both versions                           |
| PostgreSQL 16+                    | Required for workflow state, metrics, and events   |
| Qdrant + active alias             | Required for dense retrieval, including dry-run preflight |
| Blob root directory               | Required for content-addressed payload storage     |
| `psycopg`                         | PostgreSQL driver (installed via `requirements-research-store.txt`) |
| `psutil`                          | Required for process-scoped CPU telemetry           |
| `pynvml` (optional)               | Enables GPU memory sampling for performance telemetry |

### Database schema

The strict campaign requires the following tables to exist (created by
Alembic migrations):

- `research_runs` — workflow state and lifecycle transitions
- `search_candidates` — candidate recall and source quality
- `coverage_events` — coverage completeness
- `evidence_packets` — report quality
- `semantic_calls` — performance metrics (tokens, latency, model)
- `run_cache_events` — run-scoped cache hit/miss events (authoritative)
- `endpoint_usage_records` — token counts from endpoint responses
- `resource_samples` — CPU/GPU multi-sample telemetry
- `resource_summary` — aggregated CPU/GPU summary per run
- `research_claims` — claim support/contradiction state
- `claim_evidence_links` — citation accuracy

Run `"<skill-root>/scripts/research-db" migrate` to ensure the schema is up to
date before executing a campaign.

---

## 3. Command-line reference

```
strict_benchmark [--campaign-dir DIR] [--dataset PATH] [--database-url URL]
                 [--blob-root PATH] [--qdrant-url URL] [--qdrant-api-key KEY]
                 [--objectives OBJ1,OBJ2,...] [--tolerance FLOAT]
                 [--manifest PATH] [--dry-run]
```

| Argument            | Default                              | Required | Description                              |
| ------------------- | ------------------------------------ | -------- | ---------------------------------------- |
| `--campaign-dir`    | `/tmp/firecrawl_strict_campaign`     | No       | Directory for campaign artifacts         |
| `--dataset`         | `tests/fixtures/benchmark/benchmark-v1.json` | No | Benchmark dataset JSON file            |
| `--database-url`    | `$DATABASE_URL`                      | **Yes**  | PostgreSQL connection string             |
| `--blob-root`       | `/tmp/benchmark-blobs`               | No       | Content-addressed blob store root        |
| `--qdrant-url`      | `$QDRANT_URL`                        | No       | Qdrant service URL                       |
| `--qdrant-api-key`  | `$QDRANT_API_KEY`                    | No       | Qdrant API key                           |
| `--objectives`      | All objectives in dataset            | No       | Comma-separated objective IDs            |
| `--tolerance`       | `0.15`                               | No       | Reproducibility tolerance (0.0–1.0)      |
| `--manifest`        | `<campaign-dir>/manifest.json`       | No       | Path for the final artifact manifest     |
| `--dry-run`         | `False`                              | No       | Validate configuration without executing |

### Strict mode

Strict mode is **always ON**. There is no flag to disable it. The implementation
hardcodes `strict = True` in `main()` (line ~450 of `strict_benchmark.py`).

---

## 4. Execution flow

```
┌─────────────────────────────────────────────────────────────┐
│  strict_benchmark main()                                     │
│  1. Validate inputs (dataset exists, DB URL set, tolerance)  │
│  2. Create campaign directory                                │
│                                                             │
│  ┌── Campaign A ──────────────────────────────────────────┐ │
│  │ 1. Load benchmark dataset                               │ │
│  │ 2. Build ReleaseBenchmarkConfig(strict=True)             │ │
│  │ 3. Execute ReleaseBenchmarkRunner.run()                  │ │
│  │    a. For each mode × objective:                         │ │
│  │       i.   Create research run via ResearchRunService    │ │
│  │       ii.  Execute orchestrator                          │ │
│  │       iii. Populate telemetry (endpoint usage, CPU/GPU)  │ │
│  │       iv.  Extract quality metrics from PostgreSQL       │ │
│  │       v.   Extract performance metrics from PostgreSQL   │ │
│  │       vi.  Run deterministic integrity checks            │ │
│  │ 4. Write result.json, environment.json, summary.txt      │ │
│  │ 5. Compute result.json SHA-256 hash                      │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌── Campaign B ──────────────────────────────────────────┐ │
│  │ (identical to Campaign A — same dataset, DB, blob, etc.)│ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌── Reproducibility Comparison ─────────────────────────┐  │
│  │ 1. Compare all (mode, objective) pairs                 │  │
│  │ 2. Check quality metrics within tolerance              │  │
│  │ 3. Check performance metrics within tolerance          │  │
│  │ 4. Write comparison.json, summary.txt                  │  │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌── Manifest ───────────────────────────────────────────┐  │
│  │ 1. Build manifest with campaign IDs, hashes, repro    │  │
│  │ 2. Write manifest.json                                │  │
│  └───────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌── Exit Decision ──────────────────────────────────────┐  │
│  │ Exit 1 if:                                             │  │
│  │  - Campaign A or B recommends NO_GO                    │  │
│  │  - Reproducibility comparison fails                    │  │
│  │ Exit 0 otherwise                                       │  │
│  └───────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### Telemetry population order

A critical detail (fixed in commit `255be4e`) is that telemetry population
happens **before** metric extraction:

```
ResourceSampler.begin_window()
  → orchestrator.run()
  → ResourceSampler.end_window()
  → _populate_endpoint_usage()       # writes endpoint_usage_records
  → _persist_resource_samples()       # writes exact-window samples
  → telemetry_svc.build_summary()     # writes resource_summary
  → metric_engine.extract_quality_metrics()
  → metric_engine.extract_performance_metrics()
```

If telemetry is populated after extraction, strict mode will produce
null metrics with clear formulas documenting the empty source — it no
longer raises `RuntimeError`. This ensures campaigns can complete even
when the orchestrator produces partial state (coverage events but no
claims/evidence/telemetry).

---

## 5. Artifact structure

Campaign artifacts are written to `<campaign-dir>/<A|B>/<timestamp>/`:

```
<campaign-dir>/
├── A/
│   └── 20260728T230000Z/
│       ├── result.json          # Full campaign result with SHA-256 hash
│       ├── environment.json     # Runtime environment metadata
│       └── summary.txt          # Human-readable summary
├── B/
│   └── 20260728T230500Z/
│       ├── result.json
│       ├── environment.json
│       └── summary.txt
├── reproducibility/
│   └── 20260728T231000Z/
│       ├── comparison.json      # Per-metric tolerance comparison
│       └── summary.txt
├── manifest.json                # Durable artifact manifest with hashes
└── strict_benchmark.log         # (optional) stdout capture
```

### result.json schema

```json
{
  "schema_version": "release-benchmark-result-v1",
  "campaign_id": "fr_<uuid>",
  "campaign_timestamp": "2026-07-28T23:00:00+00:00",
  "environment": {
    "python_version": "3.12.3",
    "platform": "linux-x86_64",
    "machine": "x86_64",
    "processor": "unknown",
    "timestamp": "2026-07-28T23:00:00+00:00",
    "commit": "612bbd4b0887"
  },
  "recommendation": {
    "outcome": "go|go_with_conditions|no_go",
    "supported_claims": ["quality thresholds met for all workflow modes"],
    "withdrawn_claims": [],
    "known_limitations": ["CPU-based embedding..."],
    "conditions": [],
    "p0_regressions": []
  },
  "total_duration_ms": 123456.78,
  "runs": [
    {
      "campaign_id": "fr_<uuid>",
      "run_id": "fr_<uuid>",
      "mode": "agent_led",
      "objective_id": "obj-001",
      "quality": {
        "candidate_recall": 0.75,
        "source_quality_score": 0.80,
        "coverage_completeness": 0.65,
        "unsupported_claim_rate": 0.08,
        "citation_accuracy": 0.88,
        "report_quality_score": 0.78
      },
      "quality_metrics": [
        {"name": "candidate_recall", "value": 0.75, "status": "measured", "formula": "..."},
        {"name": "source_quality_score", "value": 0.80, "status": "measured", "formula": "..."},
        {"name": "coverage_completeness", "value": 0.65, "status": "measured", "formula": "..."},
        {"name": "unsupported_claim_rate", "value": 0.08, "status": "measured", "formula": "..."},
        {"name": "citation_accuracy", "value": 0.88, "status": "measured", "formula": "..."},
        {"name": "report_quality_score", "value": 0.78, "status": "measured", "formula": "..."}
      ],
      "performance": {
        "total_latency_ms": 15000.0,
        "total_tokens": 15000,
        "semantic_calls": 8,
        "cache_hit_rate": 0.3,
        "embedding_throughput": 50.0,
        "cpu_percent": 60.0,
        "gpu_memory_mb": 4096.0
      },
      "performance_metrics": [
        {"name": "total_latency_ms", "value": 15000.0, "status": "measured", "formula": "..."},
        {"name": "semantic_calls", "value": 8.0, "status": "measured", "formula": "..."},
        {"name": "total_tokens", "value": 15000.0, "status": "measured", "formula": "..."},
        {"name": "cache_hit_rate", "value": 0.3, "status": "measured", "formula": "..."},
        {"name": "embedding_throughput", "value": 50.0, "status": "measured", "formula": "..."},
        {"name": "cpu_percent", "value": 60.0, "status": "measured", "formula": "..."},
        {"name": "gpu_memory_mb", "value": 4096.0, "status": "measured", "formula": "..."}
      ],
      "errors": [],
      "integrity_checks": [
        {"check": "state_machine_transitions", "passed": true, "details": ""}
      ]
    }
  ]
}
```

### manifest.json schema

```json
{
  "schema_version": "campaign-manifest-v1",
  "dataset_path": "/path/to/benchmark-v1.json",
  "dataset_hash": "<sha256 of dataset file>",
  "dataset_version": "benchmark-v1",
  "commit": "612bbd4b0887",
  "timestamp": "2026-07-28T23:00:00+00:00",
  "campaign_a": {
    "campaign_id": "fr_<uuid>",
    "result_hash": "<sha256 of result.json>",
    "result_path": "/tmp/.../A/20260728T230000Z",
    "runs": 9,
    "recommendation": "go"
  },
  "campaign_b": {
    "campaign_id": "fr_<uuid>",
    "result_hash": "<sha256 of result.json>",
    "result_path": "/tmp/.../B/20260728T230500Z",
    "runs": 9,
    "recommendation": "go"
  },
  "reproducibility": {
    "all_within_tolerance": true,
    "run_a_id": "fr_<uuid>",
    "run_b_id": "fr_<uuid>",
    "details": []
  },
  "modes": ["agent_led", "autonomous_local", "deterministic_debug"]
}
```

---

## 6. Reproducibility comparison

The reproducibility comparison evaluates two campaigns (A and B) by comparing
every quality and performance metric for each `(mode, objective)` pair present
in both campaigns.

### Quality metrics compared

| Metric                  | Type    | Tolerance |
| ----------------------- | ------- | --------- |
| `candidate_recall`      | ratio   | configurable |
| `source_quality_score`  | ratio   | configurable |
| `coverage_completeness` | ratio   | configurable |
| `unsupported_claim_rate`| ratio   | configurable |
| `citation_accuracy`     | ratio   | configurable |
| `report_quality_score`  | ratio   | configurable |

### Performance metrics compared

| Metric | Type | Tolerance |
| ----------------------- | ------- | --------- |
| `total_latency_ms` | absolute | configurable |
| `total_tokens` | absolute | configurable |
| `semantic_calls` | absolute | configurable |
| `cache_hit_rate` | ratio | configurable |
| `embedding_throughput` | absolute | configurable |
| `cpu_percent` | absolute | configurable |
| `gpu_memory_mb` | absolute | configurable |

### Relative difference formula

For each metric pair `(val_a, val_b)`:

```
denom = abs(val_a) if abs(val_a) > 1e-9 else 1.0
rel_diff = abs(val_b - val_a) / denom
within = rel_diff <= tolerance
```

### Mode/objective set mismatch

If the two campaigns exercise different `(mode, objective)` pairs, the
comparison **fails** with an explicit detail message listing which pairs are
present in one campaign but not the other. An empty pair set in both campaigns
also fails.

---

## 7. Recommendation logic

The `_build_recommendation` method in `ReleaseBenchmarkRunner` evaluates the
campaign results and produces a `ReleaseRecommendation`:

```
IF integrity_regression:
    → p0_regressions.append("deterministic integrity check failed")

IF any quality threshold violated:
    → withdrawn.append(f"{metric} >= {threshold} — {mode} achieved {value}")

IF any performance threshold violated:
    → withdrawn.append(f"{metric} <= {threshold} — {mode} achieved {value}")

IF withdrawn:
    → outcome = NO_GO
ELIF p0_regressions:
    → outcome = NO_GO
ELIF conditions (e.g., token ratio exceeded):
    → outcome = GO_WITH_CONDITIONS
ELSE:
    → outcome = GO
```

### Quality thresholds (from benchmark dataset)

| Threshold key                | Default | Description                    |
| ---------------------------- | ------- | ------------------------------ |
| `min_candidate_recall`       | 0.5     | Minimum recall vs ground truth |
| `min_source_quality_score`   | 0.7     | Minimum source quality         |
| `min_coverage_completeness`  | 0.5     | Minimum coverage completeness  |
| `max_unsupported_claim_rate` | 0.15    | Maximum unsupported claim rate |
| `min_citation_accuracy`      | 0.8     | Minimum citation accuracy      |

### Known limitations (always documented)

- CPU-based embedding and reranking causes high latency (~8.5s per batch)
- GPU is reserved for local LLM agents; embedding/reranker run on CPU
- Local embedding models may have lower recall than OpenAI
- Local reranker may be slower than cloud alternatives

---

## 8. Failure modes and recovery

### Null metrics — missing or partial telemetry

**Cause:** The orchestrator failed partway through and did not produce
telemetry data (no claims, no evidence, no cache events, no resource
samples).

**Correction (issue #158):** Strict mode no longer produces `0.0` metrics
with formulas documenting the empty source. Instead, each metric carries
an explicit `status` field:

| Status | Meaning |
| -------- | --------- |
| `measured` | Authoritative source exists and the value is genuinely measured |
| `unavailable` | No authoritative source exists for this run/stage |
| `unevaluated` | Source exists but the metric was not evaluated |
| `incomplete` | Source exists but is incomplete |
| `stale` | Source exists but the data is stale |
| `invalid` | Source exists but the data is invalid |
| `not_applicable` | This metric does not apply to this mode/objective pair |

A metric with status `unavailable` or `unevaluated` has JSON value `null`,
never a sentinel zero. Strict release policy rejects any campaign
where a mandatory quality or performance metric is not `measured`.

**Recovery:** This is expected when infrastructure (Firecrawl, embedding
endpoint, reranker) is unavailable. The campaign completes with NO_GO
recommendation because quality thresholds are not met **and** mandatory
metrics are unavailable. Inspect the `result.json` for the `quality_metrics`
and `performance_metrics` arrays to see each metric's status.

### RuntimeError — simulation fallback blocked

**Cause:** The orchestrator attempted to use a simulation fallback, which is
prohibited in strict mode.

**Recovery:** This should not occur in normal operation. If it does, check that
all required infrastructure (Firecrawl, embedding endpoint, reranker) is
available and responding.

### CPU/GPU telemetry — value-provenance consistency (issue #160)

**Cause:** Prior to issue #160, the strict CPU flag was only set when
`cpu_samples == 0`.  When the telemetry tables themselves were absent
(pre-migration database), the query failed, the flag stayed ``False``, and
the legacy ``_legacy_cpu_percent()`` fallback executed — producing a live
host-wide ``psutil`` sample whose provenance formula claimed ``0.0``.

**Correction:** The strict CPU flag now also fires when
``telemetry_tables_exist`` is ``False``, preventing the legacy fallback.
Both CPU and GPU paths produce ``null`` with ``unavailable`` status and a
formula that documents the empty source when telemetry tables are absent.

**Verification:** Run the regression test:

```bash
pytest -q -p no:cacheprovider \
  scripts/test_performance_telemetry.py::TestStrictModeRejection::test_strict_cpu_rejects_legacy_when_tables_absent \
  scripts/test_performance_telemetry.py::TestStrictModeRejection::test_strict_gpu_rejects_legacy_when_tables_absent
```

**Expected behavior:** In strict mode with absent telemetry tables, both
``cpu_percent`` and ``gpu_memory_mb`` must be ``null`` with status
``unavailable`` and a formula identifying the missing measured samples.
No legacy ``psutil`` or ``pynvml`` samples may appear in the artifact.

**Provenance contract:** The engine uses two intermediate booleans to gate
provenance claims:

- ``_cpu_valid_samples = cpu_samples > 0 and telemetry_tables_exist``
- ``_gpu_valid_samples = gpu_samples > 0 and telemetry_tables_exist``

The metric source always names ``run_resource_samples`` and the exact run ID.
It also records sample IDs, sample count, collector/version, status counts,
and explicit device identity. Missing libraries are persisted as
``unavailable`` while collector or initialization failures are ``invalid``.

**Naming convention:** Strict-mode flags use the ``_strict_<resource>_unavailable``
pattern (``_strict_cpu_unavailable``, ``_strict_gpu_unavailable``,
``_strict_token_unavailable``, ``_strict_embedding_unavailable``).  All four
flags are ``False`` in non-strict mode and are set independently based on the
corresponding telemetry field.

### Exit 1 — campaign recommends NO_GO

**Cause:** Either campaign produced quality or performance results below
thresholds, or integrity checks failed.

**Recovery:** Inspect the `result.json` for the specific metric that fell below
threshold. Check `p0_regressions` and `withdrawn_claims` for details.

### Exit 1 — reproducibility comparison fails

**Cause:** At least one metric pair exceeded the configured tolerance between
campaigns A and B.

**Recovery:** Check the `reproducibility/comparison.json` for the specific
metric that exceeded tolerance. Investigate environmental differences between
campaigns (e.g., endpoint response times, cache state).

### Mode/objective set mismatch

**Cause:** Campaign A and B exercised different `(mode, objective)` pairs.

**Recovery:** Verify that both campaigns use the same `--objectives` argument
and that no mode execution failed silently.

---

## 9. CI integration

The `strict-campaign` CI job (defined in `.github/workflows/ci.yml`) provides
automated verification:

```yaml
strict-campaign:
  name: Strict Campaign (issue #144)
  runs-on: ubuntu-latest
  timeout-minutes: 30

  steps:
    - checkout
    - setup-python 3.12
    - install dependencies
    - start disposable PostgreSQL
    - run strict campaign unit tests
    - run strict campaign integration tests
```

### CI test categories

| Test class                           | Scope                    | Requires DB |
| ------------------------------------ | ------------------------ | ----------- |
| `TestStrictModeMandatory`            | Unit — strict mode flag  | No          |
| `TestArtifactDurability`             | Unit — JSON/hash writing | No          |
| `TestEnvironmentManifest`            | Unit — env metadata      | No          |
| `TestCLIParsing`                     | Unit — CLI arguments     | No          |
| `TestNO_GOEnforcement`               | Unit — NO_GO logic       | No          |
| `TestReproducibilityComparison`      | Unit — comparison logic  | No          |
| `TestStrictCampaignIntegration`      | Integration              | Yes         |

### Performance telemetry strict-mode tests (issue #160)

| Test | Scope | Infrastructure |
| ------ | ------- | -------------- |
| `TestStrictModeRejection.test_strict_mode_produces_zero_metrics_when_telemetry_tables_absent` | Unit — pre-migration DB | No |
| `TestStrictModeRejection.test_strict_mode_blocks_legacy_fallbacks` | Unit — empty telemetry tables | No |
| `TestStrictModeRejection.test_strict_cpu_rejects_legacy_when_tables_absent` | Unit — CPU strict flag + tables absent | No |
| `TestStrictModeRejection.test_strict_gpu_rejects_legacy_when_tables_absent` | Unit — GPU strict flag + tables absent | No |
| `TestStrictModeRejection.test_strict_cache_metric_status_is_unavailable` | Unit — cache status | No |
| `TestStrictModeRejection.test_strict_cache_metric_source_never_points_to_semantic_cache` | Unit — cache provenance | No |
| `TestStrictModeRejection.test_non_strict_cache_metric_status_is_measured_with_lookups` | Unit — non-strict cache | No |
| `TestStrictModeRejection.test_read_telemetry_sets_false_when_query_fails` | Unit — `_read_telemetry` | No |

### Integration test behavior

The integration test `test_strict_campaign_with_real_db` connects to a disposable
PostgreSQL container and verifies that strict mode correctly produces
null metrics with explicit `incomplete`/`unavailable` status when the
database is empty — not `RuntimeError`. The campaign completes with NO_GO
because quality thresholds are not met **and** mandatory metrics are
unavailable.

The `test_strict_campaign_artifacts_written` test is skipped by default because
it requires full infrastructure (Firecrawl, embedding, reranking, Qdrant, local
LLM). To run it:

```bash
export RESEARCH_STORE_TEST_DATABASE_URL="postgresql://..."
python -m pytest scripts/test_strict_campaign.py::TestStrictCampaignIntegration::test_strict_campaign_artifacts_written -v
```

---

## 10. Troubleshooting

### "psycopg is required for metric extraction"

Install the PostgreSQL driver:

```bash
pip install psycopg
```

Or ensure `requirements-research-store.txt` is installed:

```bash
pip install -r requirements-research-store.txt
```

### "dataset not found"

Verify the dataset path:

```bash
ls tests/fixtures/benchmark/benchmark-v1.json
```

Or specify a custom dataset:

```bash
strict_benchmark --dataset /path/to/custom-benchmark.json
```

### "database URL is required"

Set the `DATABASE_URL` environment variable or pass `--database-url`:

```bash
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
strict_benchmark
```

Or:

```bash
strict_benchmark --database-url "postgresql://user:pass@host:5432/dbname"
```

### "tolerance must be between 0.0 and 1.0"

The `--tolerance` flag accepts values in the range [0.0, 1.0]. A tolerance of
0.0 requires exact metric matches; 1.0 allows 100% relative difference. The
default is 0.15 (15% relative difference).

### "no runs in either campaign — cannot compare reproducibility"

Both campaigns produced zero runs. This typically means:

1. The database connection failed silently.
2. All mode executions raised exceptions.
3. The benchmark dataset has no objectives.

Check the campaign logs for specific error messages.

---

## See also

- `references/research-store-architecture.md` — system architecture and authority
- `references/research-store-operations.md` — general operations runbook
- `references/budget-policy.md` — resource budget policy
- `references/workflow-state-schema.md` — workflow state machine schema
- `scripts/research_store/strict_benchmark.py` — CLI implementation
- `scripts/research_store/release_benchmark.py` — benchmark runner implementation
- `scripts/test_strict_campaign.py` — test suite
