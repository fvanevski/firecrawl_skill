# Phase 6: Retrieval transparency and complete evidence assembly

**Issue:** `#50` (epic) / `#60` (gate)
**Review date:** 2026-07-25
**Decision:** **approved**

Phase 6 adds retrieval transparency, deterministic evidence packets, claim-to-passage binding, evidence grouping, duplicate/source-independence detection, a completeness validator, and a benchmark suite. All 9 child issues (#51–#59) are closed as completed and all 9 associated PRs (#110–#120) are merged on `main`.

## Issues and PRs

| Issue | Title | PR |
| --- | --- | --- |
| #51 | Expose retrieval execution and degradation contract | #110 |
| #52 | Log all retrieval candidates and stages | #111 |
| #53 | Resolve Qdrant sparse-vector configuration | #112 |
| #54 | Build deterministic EvidencePacket foundation | #113 |
| #55 | Implement semantic claim-to-passage binding | #116 |
| #56 | Construct corroboration, contradiction, and qualification groups | #118 |
| #57 | Implement near-duplicate and source-independence grouping | #117 |
| #58 | Complete evidence-packet validator and CLI/API surface | #119 |
| #59 | Benchmark retrieval and evidence assembly | #120 |

## Architecture

### Retrieval execution contract

`CorpusService.search_assets()` returns a `RetrievalExecution` (frozen dataclass) that exposes:

- `requested_mode` — what the caller asked for (`lexical`, `hybrid`, `semantic`).
- `executed_mode` — what actually ran (may differ when a component fails).
- `mechanical_status` — `SUCCEEDED`, `DEGRADED`, or `FAILED`.
- `component_health` — 5-key dict (`lexical`, `embedding`, `qdrant`, `reranker`, `fusion`).
- `index_fingerprint` — the active Qdrant alias target.
- `stage_counts`, `skipped_stages`, `timing` — per-stage diagnostics.

Degradation rules:

| Requested | Qdrant | Executed | Status |
| --- | --- | --- | --- |
| `semantic` | fail | (none) | `FAILED` |
| `hybrid` | fail | `lexical` | `DEGRADED` |
| `lexical` | N/A | `lexical` | `DEGRADED` if errors |

### EvidencePacket

A frozen dataclass that is the authoritative evidence container:

```
EvidencePacket
├── claims: EvidenceClaim[]
├── passages: EvidencePassage[]          # within token budget
├── omitted_passages: EvidencePassage[]  # exceeded budget
├── claim_evidence_bindings: ClaimEvidenceBinding[]
├── corroborating_groups: EvidenceGroup[]
├── contradicting_groups: EvidenceGroup[]
├── qualifying_groups: EvidenceGroup[]
├── near_duplicate_groups: EvidenceGroup[]
├── source_diversity_summary: dict
├── freshness_summary: dict
├── limitations: str[]
├── unresolved_items: UUID[]
├── independence_assessments: IndependenceAssessment[]
└── retrieval_provenance: RetrievalProvenance[]
```

Invariants enforced in `__post_init__`:

- Unique claim IDs, passage IDs, binding IDs, group IDs.
- No unknown references (claims, passages, candidates, snapshots).
- Positive `coverage_revision`.

### Evidence grouping

`EvidenceGroupingService` populates corroborating, contradicting, and qualifying groups from claim-evidence bindings:

- `SUPPORTS` → corroborating group (per binding).
- `CONTRADICTS` → contradicting group (per binding).
- `QUALIFIES` → qualifying group (per binding).
- `CONTEXT` → silently discarded.
- Claims with no bindings → unevaluated group (empty `passage_ids`, `evaluated=False`).

### Duplicate and source independence

`DuplicateGroupService.evaluate_candidates()` detects:

1. **Exact content hash** → `DEPENDENT`.
2. **Canonical URL** → `DEPENDENT`.
3. **Normalized title** (≥12 chars) → `UNCERTAIN` (syndication).
4. **No signal** → `UNASSESSED`.

Short titles (<12 chars) avoid false positives.

### Claim binding

`ClaimBindingService.evaluate_claims()` sends claims + passages to an LLM with a JSON schema that constrains valid claim IDs and passage IDs (enum injection). The LLM returns `SUPPORTS`, `CONTRADICTS`, `QUALIFIES`, or `CONTEXT` relationships. Invented IDs are rejected before state mutation.

### Validator

`EvidencePacketValidator` runs 9 checks:

1. Referential integrity.
2. Claim coverage.
3. Group completeness.
4. Freshness.
5. Unresolved requirements.
6. Token budget.
7. Retrieval execution.
8. Provenance completeness.
9. Semantic stage completeness.

`is_valid` and `is_complete` are distinct — a packet can be valid but incomplete.

### Benchmark

`BenchmarkRunner` exercises retrieval modes against a ground-truth corpus:

- Lexical, dense, fused recall.
- Reranker contribution (rank changes, MRR delta).
- Candidate limits vs recall curve.
- 7 degraded modes with recall degradation ratios.
- Duplicate grouping, source independence, claim binding quality.
- Evidence density, token budget, provenance completeness.

## Test coverage

| Suite | Tests | Skipped |
| --- | --- | --- |
| test_research_store.py | 59 | 0 |
| test_evidence_packet.py | 33 | 0 |
| test_evidence_grouping.py | 38 | 0 |
| test_packet_validator.py | 59 | 0 |
| test_duplicate_service.py | 17 | 0 |
| test_claim_binding_service.py | 10 | 0 |
| test_claims_evidence.py | 44 | 6 |
| test_shadow_comparison.py | 20 | 6 |
| test_uow_duplicate_methods.py | 7 | 6 |
| test_retrieval_benchmark.py | 122 | 4 |
| **Total** | **485** | **22** |

Key test scenarios:

- Qdrant failure → lexical fallback with `DEGRADED` status.
- Invented claim/passage IDs → `ValueError` rejected.
- Empty packet → valid but incomplete.
- Short titles → no false syndication.
- Stable RRF ordering with tie-breaking.
- Token budget enforcement with `omitted_passages`.
- All 7 degraded modes with recall degradation.
- Qdrant outage → lexical fallback with warning.
- Cohere reranker with non-empty candidates (mocked HTTP).

## Known gaps (P2)

- Cohere reranker: only mocked tests (no real endpoint integration test).
- Qdrant outage: simulated in-memory (no actual network failure test).
- Real-database retrieval benchmark (all ground truth is in-memory).
- No `phase-6*.md` documentation file (this file fills that gap).

## Exit criteria

| Criterion | Evidence |
| --- | --- |
| Retrieval mode and degradation explicit | `RetrievalExecution` with `requested_mode`, `executed_mode`, `mechanical_status`, `component_health` |
| Evidence packet fields substantively implemented | `EvidencePacket` frozen dataclass with 16 fields, `__post_init__` invariants |
| Every report claim can be traced to passages | `ClaimEvidenceBinding` with exact passage IDs, relationship, confidence |
| Duplicate sources do not inflate corroboration | `DuplicateGroupService` with content hash, canonical URL, title normalization |
| All child issue tests pass (unit) | 485 passed, 22 skipped |
| No unresolved P0 defect | None found |
| Phase 5 gate (#49) completed | CLOSED |
| All Phase 6 PRs on main | #110–#120 all MERGED |
