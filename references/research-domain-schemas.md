# Research workflow domain schemas

The canonical phase-1 workflow contracts live in `scripts/research_domain/`.
They are immutable typed Python models whose JSON Schemas are generated into
`schemas/research-workflow/`. They do not perform model transport, database
writes, orchestration, budget selection, or workflow transitions.

## Registered versions

| Contract | Current write version | Readable versions |
| --- | --- | --- |
| `ResearchSpec` | `research-spec-v1` | `research-spec-v1` |
| `SearchPlan` | `search-plan-v1` | `search-plan-v1` |
| `CandidateAssessment` | `candidate-assessment-v1` | `candidate-assessment-v1` |
| `CoverageLedger` | `coverage-ledger-v1` | `coverage-ledger-v1` |
| `StrategyRevisionProposal` | `strategy-revision-v1` | `strategy-revision-v1` |
| `EvidencePacket` | `evidence-packet-v1` | `evidence-packet-v1` |

These are the first canonical versions, so there are no predecessor versions
or legacy field aliases. Existing Catalog v5 and `research_workflow.py`
structures remain separate compatibility formats. A future version may be read
compatibly only after its conversion is explicit, deterministic, tested, and
registered. Writers always emit the current exact version.

## Validation boundary

`load_model` performs strict structural decoding: missing and unknown fields,
invalid UUIDs, unsupported enums, invalid ranges, duplicate stable IDs, and
unsupported schema versions are rejected. `ValidationContext` adds deterministic
cross-document checks for:

- query and assessment references to ResearchSpec questions and claims;
- candidate, snapshot, and passage IDs known to the caller;
- coverage subjects mapped to the correct requirement type;
- strategy targets mapped to the current coverage revision;
- exact run and ResearchSpec identities;
- stale run or coverage revisions;
- evidence bindings, groups, and retrieval events mapped to packet passages.

Validation proves structural and referential validity only. It does not declare
a proposal semantically correct or authorize state mutation.

## Semantic versus mechanical state

Semantic uncertainty uses explicit semantic fields such as `uncertain`,
`unassessed`, `remaining_gap`, and binding uncertainty. Mechanical execution
uses `MechanicalStatus` and structured `MechanicalFailure` records. The two
enums are disjoint: `failed` is not a coverage status, and `uncertain` is not a
mechanical status. A successful retrieval cannot carry component errors, while
a degraded, failed, or unavailable retrieval must carry an error record.

## Stable serialization and schema generation

`serialize_model` emits JSON-compatible dictionaries. `dumps` sorts object keys
and uses stable separators, so semantically identical models serialize to the
same bytes. Tuple order remains meaningful and is preserved.

Regenerate the checked-in schemas from the repository root with:

```bash
rtk python3 -c 'from pathlib import Path; from scripts.research_domain.registry import write_schemas; write_schemas(Path("schemas/research-workflow"))'
```

Review generated diffs as API changes. Do not update schemas independently of
the typed models, and do not accept a generated diff without corresponding
valid, invalid, referential, revision, and compatibility tests.

## Compatibility and repair

Adding optional meaning to a v1 field still requires deterministic defaults and
tests before older v1 documents may omit it. Renaming, removing, or changing a
field or enum requires a new schema version and an explicit reader/converter.
Unknown versions fail closed.

This issue creates no PostgreSQL migration. Rolling it back means reverting the
domain package, generated schemas, fixtures, tests, and this document. If a
published contract needs repair, add a new version and forward converter rather
than silently rewriting retained semantic artifacts.
