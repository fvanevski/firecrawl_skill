# Issue #311 independent-review remediation authority map

This reference records the central remediation performed after independent review of pull request #317. It is documentary evidence only: current source, tests, PostgreSQL state, Git identity, and exact-head CI remain authoritative.

## Scope

Issue #311 requires semantic model assistance to stop at bounded semantic proposals/labels. Application code must own operational provider mechanics, deterministic query order and priority, temporal admission, candidate selection, resource budgets, lifecycle, replay, and coverage authority.

The independent review at commit `658bbd36a9b1bad2c7b6fd2f5b7ecb4a42e071b8` identified three blocking authority leaks. Subsequent remediation also hardened adjacent compatibility and validation surfaces exposed by automated inline review. The original review is stale after later commits; each final conclusion must be rebound to the then-current exact PR head.

## Blocking finding 1 — semantic facet selected provider scrape transport

**Violated invariant:** A semantic field may describe research purpose, but may not select a provider operation or force scrape admission.

**Root cause:** The previous production acquisition stage translated `facet == "benchmark_source"` into `backend="firecrawl_scrape"` before temporal admission or deterministic candidate selection.

**Resolution:** Canonical production composition injects the planned deterministic acquisition stage. That stage always performs discovery with `backend="firecrawl"`; `facet` remains semantic metadata only. Candidate scrape/extraction scheduling occurs after temporal admission and deterministic selection. The planned temporal acquisition service also separates provider result volume (`limit`) from deterministic candidate-selection capacity (`selection_limit`).

**Regression authority:** `tests/unit/test_issue311_acquisition_review_remediation.py` includes an adversarial `facet="benchmark_source"` query and requires the provider backend to remain `firecrawl`.

## Blocking finding 2 — model proposal order became query priority and budget order

**Violated invariant:** Reordering a semantically identical proposal set must not change deterministic persisted priority/order or which branch receives bounded execution precedence.

**Root cause:** The previous query materializer assigned priority while traversing model output. PostgreSQL then persisted that order, and acquisition consumed branch/extraction capacity in the same sequence.

**Resolution:** `query_policy.materialize_query_plan` validates the complete bounded proposal set before operational materialization, canonicalizes semantic fields, sorts by a deterministic content-derived key, and only then assigns priority and query identity. An over-cap model response fails closed instead of being traversal-order truncated. The production planned-acquisition stage executes persisted plan queries by deterministic persisted priority/query ID and orders adaptive additions independently of traversal order.

**Regression authority:** `tests/unit/test_issue311_review_remediation.py` proves reversed proposal input yields the identical materialized plan and priorities, and proves an over-cap response fails before order-dependent truncation.

## Blocking finding 3 — acquisition ignored the persisted authoritative resource budget

**Violated invariant:** Once a planning bundle is persisted, acquisition/selection must use that run's PostgreSQL-backed budget snapshot and durable progress counters rather than re-evaluating current policy/configuration.

**Root cause:** The previous stage called `DEFAULT_POLICY.evaluate(...)` during acquisition, used provider `limit` as the candidate-selection cap, and applied extraction limits later in orchestration state.

**Resolution:** Planned acquisition requires `context.authoritative_budget.effective_caps`, reads durable extraction-attempt and search-response state through the canonical unit of work, validates persisted counts against the snapshot, reconstructs already-executed queries, and computes remaining global extraction capacity before each branch. `DeterministicPlannedTemporalAcquisitionService` uses a separate `selection_limit` for deterministic post-temporal candidate selection. No current-policy evaluation occurs for this persisted planned-acquisition path.

**Regression authority:** `tests/unit/test_issue311_acquisition_review_remediation.py` seeds persisted attempts and an already-executed query, verifies the remaining candidate-selection limit is reduced globally, verifies restart skips the completed branch, and verifies missing persisted budget authority fails closed.

## Automated-review and important non-blocking hardening

The following adjacent changes prevent alternate compatibility paths from reintroducing authority leaks:

- Query proposal validation no longer coerces arbitrary values through `str(...)`; declared semantic string/list types are enforced before parsing/materialization.
- Explicit `ResearchSpec.objective` and a supplied `fsearch_smart` topic must agree exactly, preventing two competing objective authorities.
- Legacy candidate triage derives stable candidate IDs without traversal indexes, sorts candidates deterministically before bounded batching, and validates semantic-label payloads even on the compatibility surface.
- Without persisted ResearchSpec authority, legacy semantic labels cannot invent target question IDs.
- Production composition keeps the established `BoundedAcquisitionStage` wiring contract name while lazily binding it to the deterministic planned implementation, avoiding an import cycle and preserving orchestration composition contracts.

The four GitHub inline automated-review threads are retained as review records. Three are outdated after source changes; one remains attached to `scripts/fsearch_smart`. Their comment bodies were not exposed by the available focused review-state API during central remediation, so this reference does not invent or paraphrase unavailable text. Exact thread disposition remains an independent-review/readback task.

## Test and documentary authority

The dedicated `.github/workflows/deterministic-planning-selection-review.yml` exact-head gate includes the planned-acquisition/composition paths and both independent-review remediation unit suites. It runs static checks plus Python 3.11/3.12 service-backed runtime authorities using repository-sanctioned disposable PostgreSQL/Qdrant helpers.

`tests/contract/test_workflow.py` is the active contract authority for the current deterministic, non-predictive `fsearch_smart --dry-run` preview. The similarly named scratch-artifact check in `scripts/fixtures/workflow_test_cases.py` is an historical fixture module and is not a normative contract; it must not be used to infer that current dry-run execution writes diagnostic scratch artifacts.

## Closure criteria before local-agent handoff

Central work is ready for host-bound validation only when all of the following are true at one exact head SHA:

1. Ruff, Ruff format, and full-project Pyrefly are green without new suppressions that bypass policy.
2. The dedicated deterministic-planning static and Python 3.11/3.12 runtime jobs are green.
3. Broad CI and relevant acquisition/orchestration/fsearch/fscrape/storage authority gates are green or any failure is explicitly proven unrelated to the change surface.
4. PR documentation names the current exact head and does not claim stale validation evidence.
5. The branch head is re-read immediately before issuing the local-agent handoff.
