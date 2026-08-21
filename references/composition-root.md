# Research-store composition root

This document is the **final Phase-5 composition contract**, including the
exact-main gate remediation after issue #266 was evaluated at
`b6700a06e897b6651f737de4994b1acae8d4d2ca`.

## Governing rule

`research_store.composition` is the only general production composition root.
Application/domain implementation must not depend back on it. Function-local
imports do not exempt an edge from this rule.

A separate `production_topology` module remains allowed only as a narrow leaf
that supplies `ProductionBoundedExtractionStage`. It may not resolve
`StoreConfig`, construct UoWs/services, own workflow policy, or import
`composition`.

```text
operator / CLI boundary
        -> research_store.composition
             -> application services / repositories / infrastructure
             -> application orchestrators with injected collaborators
             -> production_topology (bounded extraction leaf only)
```

Forbidden:

```text
application/domain implementation -> composition
application/domain implementation -> migration composition facade
application service -> config-driven self-construction of production dependencies
orchestrator subclass -> build(...) production factory
```

## Canonical ownership

| Responsibility | Final owner |
|---|---|
| UoW factory | `research_store.composition.build_uow_factory` |
| Store/service builders | `research_store.composition` |
| `fscrape` production construction | `research_store.composition.build_fscrape_service` |
| policy-complete `fsearch` construction | `research_store.composition.build_fsearch_service` / `build_policy_fsearch_service` |
| inspection production construction | `research_store.composition.build_inspection_service` |
| fresh/resumable production orchestration | `research_store.composition` |
| generic orchestrator assembly | `research_store.composition.build_orchestrator_instance` |
| bounded extraction adapter leaf | `research_store.production_topology` |
| direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` |
| smart-search budget/plan/provenance application behavior | `research_store.smart_search_application` |
| operator UoW helpers | `research_store.store_runtime`, `research_store.index_admin` reusing the canonical factory |

`handoff_admin.build_handoff` remains distinct because its injectable UoW
constructor has a different signature; it is not an equivalent composition
root.

## Application injection rules

### Orchestration

`ResearchOrchestrator`, `CheckpointResearchOrchestrator`, and
`ProvenanceResumableResearchOrchestrator` do **not** expose config-driven
`build(...)` classmethods. The root constructs run/coverage/strategy/acquisition,
corpus/extraction/evidence, terminal, and stage dependencies and passes them to
the application object.

This supersedes the earlier Phase-5 compromise where historical orchestrator
builders imported `production_topology` themselves. The leaf remains valid, but
its injection now occurs from the canonical root.

### Run audit

`ResearchRunService.trigger_audit()` does not resolve the composition root or a
concrete audit service. `build_run_service()` injects the audit-service factory;
a manually constructed run service fails closed when that production dependency
was not supplied.

### `fscrape`, `fsearch`, and inspection

Application modules contain application behavior only. Production builders are
root-owned. Public CLI modules are allowed root consumers because they are
operator boundaries:

- `fscrape_cli` -> `composition.build_fscrape_service`;
- `fsearch_cli` -> `composition.build_policy_fsearch_service`;
- `inspection_cli` -> `composition.build_inspection_service`.

`fsearch_service.main()` remains an injectable CLI contract for tests but has no
production factory fallback; the production launcher executes `fsearch_cli`.

## `scripts/fsearch_smart` boundary

The extensionless executable remains a genuine operator entrypoint. It may own:

- argument parsing and output/exit behavior;
- environment/bootstrap resolution;
- operator subprocess calls;
- adaptation of top-level `scripts/research_workflow.py` planner tooling;
- calls into the canonical production composition root.

Reusable application behavior does **not** remain there. The following are owned
by `research_store.smart_search_application`:

- budget evaluation;
- deterministic fallback queries;
- canonical `search-plan-v1` normalization;
- planning-bundle initialization;
- planner provenance persistence and commit.

The operator script must not directly call UoW `append_event`/`commit` for that
workflow.

## Removed migration and compatibility surfaces

Final topology does not retain `research_store.container`,
`research_store.orchestration.composition`, or
`research_store.acquisition.direct_scrape`. It also does not retain application-
owned production builder delegates merely to preserve old import paths. Callers
must migrate to the final root or application owner.

## Wiring-only invariant

`composition.py` may construct and connect dependencies. It must not own SQL,
transactions, workflow transitions, provider execution, policy evaluation, or
application class bodies. Structural regressions reject representative
persistence/workflow calls and require the canonical eight-field
`PostgresUnitOfWork` partial to have exactly one owner.

## Mechanical enforcement

`tests/unit/test_issue_267_composition_root.py` now enforces all of the following:

- the equivalent UoW partial exists only in `composition.py`;
- enumerated application/service modules have no static or function-local import
  of `composition`;
- root-owned production builder names occur only in `composition.py`;
- the three orchestrator application classes expose no `build` factory;
- `production_topology.py` remains a narrow leaf;
- the root remains wiring-only.

`tests/contract/test_phase5_gate_remediation.py` separately enforces the
`fsearch_smart` operator/application boundary and continues to derive the retired-
module inventory from the #269 final-topology authority rather than duplicating
Codex-review-sensitive string literals.

## Validation

Final exact-head validation requires native Git identity, Serena `no-memories`
caller/dependency census, Ruff, changed-scope and full-project pinned Pyrefly,
focused composition/topology/remediation tests, structural metrics, broader
repository tests with skip classification, and disposable PostgreSQL/Qdrant tests
where mutation is involved. No test, baseline, checker scope, suppression,
runtime authority, or release gate may be weakened to obtain green.
