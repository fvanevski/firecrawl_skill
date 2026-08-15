# Structural Refactor Contract

## Status and authority

The semantic-locality guidance codified in this document is the structural authority for the refactor campaign tracked by epic #246. It governs topology, module and symbol boundaries, dependency direction, compatibility facades, and structural review heuristics.

`.refactor/PRD.md` remains the behavioral authority. If this document conflicts with the PRD on runtime behavior, persistence semantics, workflow authority, public schema or CLI semantics, transactions, provenance, validation, security, release gates, or compatibility requirements, the PRD wins. Structural changes must adapt to the behavioral contract; they must not reinterpret it.

Issue and gate acceptance criteria may impose narrower constraints for their owned scope. They do not silently supersede either authority source.

## Structural objective

Optimize for semantic locality and bounded agent changes, not minimum file size. Cohesive behavior should live behind named, typed boundaries so a normal change can be understood and edited by inspecting a small set of relevant symbols and principal files.

Prefer capability-oriented vertical slices with explicit ports at infrastructure boundaries. Source movement is incremental. Large-scale relocation without a demonstrated boundary is not an objective.

## Preservation invariants

Unless an issue explicitly requires a behavioral change and maps that change back to the PRD, structural work must preserve all of the following.

### Persistence and authority

- PostgreSQL remains the sole authoritative durable workflow, lifecycle, identity, metadata, provenance, corpus, evidence, audit, and durable-job state.
- `BLOB_ROOT` remains the authoritative immutable content-addressed byte store under the current Target A contract. PostgreSQL metadata must reference installed immutable payload bytes by their validated identity/digest boundary.
- Qdrant remains a rebuildable retrieval projection. Its contents, aliases, or collection state must never become workflow or corpus authority.
- Valkey remains transient notification, coordination, and bounded-cache state. Loss of Valkey must remain recoverable from PostgreSQL state.
- Ephemeral files, scratch data, Catalog-compatible exports, and presentation artifacts remain derived/non-authoritative unless the PRD explicitly changes that contract.

### Schema and serialized contracts

- Public schema versions and serialized meanings do not change merely because code moves.
- Stable identifiers retain their existing meaning and validation rules.
- Compatibility reads may be retained only where the current campaign requires them; compatibility behavior must not become a second authority.
- A structural refactor must not fabricate historical provenance, lifecycle state, exact-membership evidence, or semantic decisions.

### CLI and wrapper contracts

- Existing CLI command names, options, output contracts, exit semantics, and authority boundaries remain unchanged unless the implementing issue explicitly changes them.
- Thin wrappers may delegate to canonical modules, but delegation must preserve parser and failure behavior.
- Removed compatibility surfaces are not to be reintroduced merely to reduce refactor effort.

### Migration authority

- Alembic remains the sole schema-migration authority. At the baseline SHA, the documented clean schema head is `0040_asset_promotion_membership`; this issue does not change it.
- Structural movement must preserve current migration path discovery and current-head behavior.
- Existing forward-only/forward-repair rules remain intact. No manual schema stamping, compatibility table resurrection, or migration bypass is permitted as a structural shortcut.
- Migration modules may move only when Alembic configuration, version discovery, and upgrade behavior are proven equivalent.

### Transactions, concurrency, and provenance

- Existing transaction boundaries, rollback behavior, savepoints, idempotency, locking, lease ownership, lifecycle compare-and-swap rules, outbox/durable-job behavior, and exact-membership checks remain unchanged unless an issue explicitly targets them.
- Authoritative success must still be reported only after the corresponding PostgreSQL commit succeeds.
- Blob installation and PostgreSQL metadata semantics remain at the current logical boundary: a rolled-back metadata transaction cannot create an authoritative corpus record, and missing/digest-mismatched referenced bytes remain corruption.
- Qdrant projection writes remain idempotent/rebuildable and cannot upgrade projection state into authoritative state.

### Release, validation, and security

- Existing unit/integration tests, Ruff checks, skip classification, release evidence, exact-head checks, security validation, and release gates remain requirements.
- Structural work must not weaken assertions, validation, test selection, skip policy, release policy, provenance checks, or security boundaries to obtain green CI.
- A failing gate is evidence to resolve, not a reason to bypass the gate.

## Desired dependency direction

The target dependency direction is:

```text
entrypoints / tools
        |
        v
application / orchestration / domain services
        |
        v
typed domain contracts and ports
        |
        v
infrastructure adapters
(PostgreSQL, BLOB_ROOT, Qdrant, Valkey, Firecrawl, model endpoints)
```

Rules:

1. Entrypoints and compatibility wrappers may depend inward on application services; core application/domain code must not import CLI parsing or wrapper modules.
2. Domain contracts and deterministic policy code must not depend directly on PostgreSQL, Qdrant, Valkey, Firecrawl, or model-client implementations.
3. Infrastructure adapters implement typed ports owned by the inward-facing layer. They may depend on external clients and storage libraries.
4. Cross-slice calls should use explicit services/contracts rather than importing another slice's persistence implementation.
5. Composition-root code is the allowed place to construct concrete adapters and inject them into application services.
6. Dependency inversion is not a mandate to add abstractions. Introduce a port when it protects an authority boundary, enables a real alternate implementation/test seam, or materially improves semantic locality.

The current tree is transitional. Later issues are expected to move toward this direction incrementally rather than enforce it through a mass migration.

## Semantic-locality review heuristic

The structural target is a cohesive change surface, not a numerical LOC gate.

For implementation modules:

- **~200–450 physical LOC**: normal target range for a cohesive module.
- **~500–600 physical LOC**: review trigger. Check whether the file contains separable responsibilities or an avoidable mixed boundary.
- **~700+ physical LOC**: strong split trigger when a cohesive boundary can be extracted without damaging locality or compatibility.
- Files outside these ranges are not automatically defective. Small modules may be justified for narrow contracts; larger modules may remain intact when splitting would increase coupling or inspection cost.

Functions commonly remain in the **~10–50 LOC** range, but the same rule applies: cohesion and explicit contracts outrank line count.

A healthy normal change should often touch roughly **2–5 implementation symbols across 2–4 principal files**. This is a design objective and review signal, not a CI threshold.

No CI job should fail solely because a module or function crosses one of these size ranges.

## Temporary compatibility-facade policy

Compatibility facades are temporary migration devices, not architectural destinations.

A facade is permitted only when all of the following hold:

1. a current public/import surface must remain available while the canonical implementation moves;
2. the facade delegates to one canonical implementation rather than duplicating behavior;
3. authority, transaction, validation, and failure semantics remain in the canonical implementation;
4. the facade contains no new domain or persistence logic beyond argument/return adaptation required by the preserved contract;
5. the implementing issue records the compatibility surface and the later issue/gate responsible for removal when removal is part of the campaign.

A facade must not be retained merely to avoid updating internal callers. Internal imports should migrate to the canonical boundary as soon as the relevant issue owns that change.

Compatibility facades may preserve imports; they may not preserve obsolete behavior that the campaign has already removed or explicitly rejected.

## Baseline structural inventory

`references/architecture-baseline.json` is the deterministic pre-refactor inventory for `main` at:

```text
c730b562343e10193fecaf4684925dcee0dc1403
```

It is generated by `tools/architecture_inventory.py`.

The inventory scope is every `scripts/**/*.py` module except:

- `test_*.py` modules;
- `conftest.py`;
- `*_test_support.py`;
- modules under `fixtures/`.

Alembic environment/version modules are included because migration topology is an architectural boundary. Maintenance/release Python modules are also included; their category distinguishes them from runtime application modules.

For each in-scope module the inventory records:

- repository-relative path;
- importable module name;
- physical LOC (`len(file_text.splitlines())`);
- deterministic architectural category;
- module-level classes/functions/async functions;
- AST-resolved imports to other in-scope modules;
- local fan-out and fan-in counts.

The import graph is intentionally limited to statically resolvable imports between inventoried modules. Dynamic imports, subprocess execution, extensionless shell/Python wrappers, dependency injection, and runtime call graphs are outside this metric and must not be inferred from it.

Regenerate the baseline with:

```bash
python tools/architecture_inventory.py \
  --root . \
  --source-sha c730b562343e10193fecaf4684925dcee0dc1403 \
  --output references/architecture-baseline.json
```

Verify a checked-in baseline without rewriting it with:

```bash
python tools/architecture_inventory.py \
  --root . \
  --source-sha c730b562343e10193fecaf4684925dcee0dc1403 \
  --check references/architecture-baseline.json
```

The generator contains no timestamp, machine path, Python-version field, or environment-derived ordering. The same source tree and source SHA therefore produce byte-stable JSON.

## How later issues use the baseline

The baseline is evidence, not an acceptance gate by itself. Later refactor PRs should compare relevant modules and dependency edges against it when that comparison helps demonstrate improved locality or reduced coupling.

Do not claim improvement from LOC reduction alone. A valid structural improvement must preserve the behavioral invariants above and should reduce mixed responsibilities, dependency inversions, or the number of principal symbols/files needed for a normal change.

Phase-gate evaluation must re-check the required behavioral and compatibility invariants against the exact gate head. A generated structural metric cannot substitute for runtime, migration, transaction, or release evidence.
