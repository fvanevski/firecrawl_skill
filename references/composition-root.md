# Research-store composition root

This document is the **final post-#269 composition contract**.

## Governing rule

`research_store.composition` is the only general production composition root.
Application/domain implementation must not depend back on it. A separate
`production_topology` module is allowed only as a narrow leaf that injects the
bounded production extraction adapter for historical orchestrator builder
semantics; it may not become a second service/UoW root.

```text
operator / entrypoint
        -> research_store.composition
             -> application services / repositories / infrastructure

checkpoint/search-provenance builders
        -> production_topology
             -> bounded extraction port + concrete adapter
```

Forbidden:

```text
application/domain implementation -> composition
application/domain implementation -> migration composition facade
```

Function-local imports do not exempt an edge from this rule.

## Canonical ownership

| Responsibility | Final owner |
|---|---|
| UoW factory | `research_store.composition.build_uow_factory` |
| Store/service builders | `research_store.composition` |
| Fresh/resumable production orchestration | `research_store.composition` |
| Bounded extraction adapter leaf | `research_store.production_topology` |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` |
| Operator UoW helpers | `research_store.store_runtime`, `research_store.index_admin` reusing the canonical factory |

`handoff_admin.build_handoff` remains distinct because its injectable UoW
constructor has a different signature; it is not an equivalent composition
root.

## Removed migration surfaces

Final topology does not retain `research_store.container`,
`research_store.orchestration.composition`, or
`research_store.acquisition.direct_scrape`. Internal callers migrate to the
canonical root/application owner. No same-object compatibility test should
require those paths to survive.

## Wiring-only invariant

`composition.py` may construct and connect dependencies. It must not own SQL,
transactions, workflow transitions, provider execution, policy evaluation, or
application class bodies. Structural regressions reject representative
persistence/workflow calls and require the canonical eight-field
`PostgresUnitOfWork` partial to have exactly one owner.

`production_topology.py` contains only `ProductionBoundedExtractionStage` and
its concrete adapter injection. It must not resolve `StoreConfig`, construct
UoWs/services, execute workflow policy, or import `composition`.

## Codex review record

No Codex-authored review/thread/comment is retrievable on PR #292's authoritative
review surfaces. A historical #267 note recorded an inline review around the old
`orchestration/composition.py` import arrangement, but its exact body was not
captured and is not treated as current PR evidence. The relevant architectural
class is nevertheless resolved by the final rule above: orchestrator builders
use `production_topology`, not a composition facade.

## Validation

Final exact-head validation requires native Git identity, caller census before
physical deletion, Ruff, changed-scope and full-project Pyrefly, focused
composition/acquisition/orchestration regressions, isolated wheel checks, and
disposable-service tests where PostgreSQL/Qdrant mutation is involved. No test,
baseline, checker scope, or runtime authority may be weakened to obtain green.

See `references/issue-269-final-cleanup.md` for the mechanical finalization
manifest.
