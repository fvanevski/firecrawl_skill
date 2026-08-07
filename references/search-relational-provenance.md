# Search relational provenance

Issue: #214

## Authority contract

PostgreSQL is the authority for provider-search lifecycle and provenance.
`search_responses` stores a direct relational link to the provider-search
`research_invocations` row for supported new acquisition writes. Qdrant is not
consulted for search provenance and remains a rebuildable projection.

A provider search is represented by a dedicated `research_invocations` row with
`operation = 'search_provider'`. When a caller already owns an invocation (for
example authoritative `fsearch`), the provider invocation records it as
`parent_invocation_id`. The response row links to the provider invocation via
`search_responses.invocation_id`.

`attempt_ordinal` records the terminal provider attempt represented by the
persisted response. Supported adapters expose the attempt count in transport
metadata; adapters without retry metadata use ordinal 1 because the persisted
response is their first observable provider attempt. The relational column is
authoritative after persistence; consumers do not reconstruct it from JSON.

## Smart planned searches

The smart-search orchestrator resolves only the exact current persisted plan and
query row. Resolution is by the current plan ID plus exact query text, with a
hard failure if the persisted plan contains duplicate matching text. There is no
fuzzy or historical text matching.

Persisted planned queries use these active states:

- `pending`
- `running`
- `succeeded`
- `empty`
- `failed`
- `cancelled`

The provider invocation and `pending -> running` transition commit before
provider execution. The response row, its invocation/attempt linkage, the
terminal plan-query state, provider-invocation terminal state, candidates, and
the acquisition event commit in one PostgreSQL transaction.

The former `executed` value is retained only as a read-only compatibility value
for any pre-existing rows. New inserts or transitions to `executed` are rejected.

Adaptive strategy queries that are not rows in the persisted search plan remain
unplanned acquisitions. They still receive provider-invocation and attempt
provenance, but they do not fabricate `plan_id` or `plan_query_id`.

## Migration and compatibility

Migration `0041_search_relational_provenance` is additive and forward-only.

Existing provider responses start as `historical_unresolved`. A historical row
is promoted to `resolved` only when its existing `transport_metadata` contains:

1. a syntactically valid invocation UUID that exists for the same research run;
2. a positive explicit `attempt` or `attempts` value; and
3. a uniquely provable `(invocation, backend, attempt ordinal)` tuple.

The migration does **not** use query text, timestamps, row order, proximity, or
other inferred signals. Ambiguous/incomplete history stays
`historical_unresolved`. Existing non-provider `backend = 'orchestrator'`
telemetry is marked `not_applicable`.

The low-level repository write API predates issue #214 and cannot accept the new
relational fields without a wider public repository-contract change. Such
compatibility writes default to `unresolved_compatibility`; supported network
acquisition through `AcquisitionService` always promotes its response to
`resolved` before commit. A row claiming `resolved` provenance is constrained to
have a same-run invocation FK and a positive attempt ordinal, and duplicate
resolved invocation/backend/plan-query/attempt tuples are rejected.

No downgrade is provided. Recovery follows the repository's forward-only
migration policy: apply a forward repair or restore PostgreSQL from the
pre-migration backup boundary.

## Validation

Issue #214 regression coverage exercises:

- direct relational provider invocation linkage;
- idempotent replay and retried attempt ordinals;
- deterministic observation of the `running` state at the provider seam;
- `succeeded`, `empty`, `failed`, and `cancelled` terminal plan-query states;
- three smart plan queries joined directly to their plan/query rows;
- same-run invocation FK and duplicate-attempt constraint rejection; and
- a guard that historical backfill contains no fuzzy text/timestamp matching.
