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
metadata. An adapter that exposes no retry metadata uses ordinal 1 because the
persisted response is its first observable provider attempt. If an adapter does
explicitly expose `attempt` or `attempts`, that value must be a positive
PostgreSQL 32-bit integer; malformed, zero, negative, or out-of-range explicit
values fail closed rather than being replaced with a fabricated ordinal.

The relational columns are authoritative after persistence. Database-native
history and replay surfaces read `search_responses.invocation_id` directly;
consumers must not reconstruct invocation identity from `transport_metadata`
JSON. Attempt, plan/query, and provenance status remain directly queryable from
their relational `search_responses` columns.

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

If provider execution raises, explicit attempt metadata is invalid, or authority
changes after provider execution but before response persistence, the already
committed running provider attempt is terminalized deterministically. A current
run cancellation wins and produces `cancelled`; otherwise the attempt and its
planned query become `failed`. Cleanup first checks for an already committed
resolved response so an uncertain commit outcome cannot overwrite authoritative
success. The terminal invocation output includes a bounded machine-readable
reason code and persisted error text is secret-redacted.

The former `executed` value is retained only as a read-only compatibility value
for any pre-existing rows. New inserts or transitions to `executed` are rejected.

Adaptive strategy queries that are not rows in the persisted search plan remain
unplanned acquisitions. They still receive provider-invocation and attempt
provenance, but they do not fabricate `plan_id` or `plan_query_id`.

## Concurrency and idempotency

Search idempotency is serialized by a PostgreSQL advisory lock scoped to the run
and idempotency key, but lock acquisition is bounded. The service uses
`pg_try_advisory_lock` with a monotonic deadline rather than an unbounded
`pg_advisory_lock` wait. The default acquisition bound is 5 seconds with a
50-millisecond polling interval. Contention that exceeds the deadline fails
closed with reason code `search_idempotency_lock_timeout`; a contender does not
invoke the provider while another attempt owns the key.

The lock is released in `finally` after rollback of any in-flight transaction.
Successful retry/replay with the same request envelope reads the already
resolved relational response and does not perform another provider call.
Conflicting idempotency-key reuse remains rejected.

## Migration and compatibility

Migration `0041_search_provenance` is additive and forward-only.

Existing provider responses start as `historical_unresolved`. A historical row
is promoted to `resolved` only when its existing `transport_metadata` contains:

1. a syntactically valid invocation UUID that exists for the same research run;
2. an explicit `attempt` or `attempts` value in the positive 32-bit integer
   range; and
3. a uniquely provable `(invocation, backend, attempt ordinal)` tuple.

The migration checks the numeric range before casting to PostgreSQL `integer`,
so malformed and arbitrarily large historical values remain unresolved instead
of aborting the migration. It does **not** use query text, timestamps, row order,
proximity, or other inferred signals. Ambiguous/incomplete history stays
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

## Audit and error output

Provider-attempt terminalization records one of these reason codes in the
provider invocation output when no authoritative response was committed:

- `provider_attempt_cancelled`
- `provider_attempt_failed_without_response`

Bounded idempotency-lock failure uses
`search_idempotency_lock_timeout`. Persisted exception details are capped and
redact common API-key, token, authorization, password, secret, credential, and
Bearer-token forms. Raw retained provider payload handling is unchanged; this
redaction rule applies to diagnostic error text, not immutable evidence bytes.

## Validation

Issue #214 regression coverage exercises:

- direct relational provider invocation linkage and idempotent replay;
- public database-native history/replay reading relational provenance rather
  than transport JSON;
- retried attempt ordinals and fail-closed invalid explicit attempt metadata;
- deterministic observation of the `running` state at the provider seam;
- `succeeded`, `empty`, `failed`, and `cancelled` terminal plan-query states;
- concurrent run cancellation after provider execution and terminal cleanup;
- the production `ProvenanceResumableResearchOrchestrator` seam for three smart
  plan queries joined directly to their persisted plan/query rows;
- bounded advisory-lock contention with no duplicate provider execution;
- same-run invocation FK and duplicate-attempt constraint rejection;
- a real pre-0041 migration containing valid, cross-run, ambiguous, malformed,
  oversized, and out-of-range historical metadata; and
- source guards confirming historical backfill contains no fuzzy
  text/timestamp matching.
