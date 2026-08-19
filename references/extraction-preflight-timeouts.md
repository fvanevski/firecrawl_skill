# Bounded candidate extraction and suitability preflight (issue #216)

Issue #216 / audit finding RC-07 separates search discovery from candidate
provider extraction. The objective is not merely to reject bad content before
corpus ingestion: a slow or empty candidate must also be unable to hold the
provider/extraction path for the approximately 609-second failure observed in
the audited run.

## Authority and lifecycle boundary

PostgreSQL remains authoritative for run lifecycle, candidate identity,
extraction attempts, and exact completion membership. `BLOB_ROOT` remains the
immutable payload/content store and Qdrant remains a rebuildable projection.
The bounded provider layer does not use Qdrant, Valkey, presentation exports,
or provisional synthesis as lifecycle or completion evidence.

The production path is deliberately split:

1. `firecrawl search` performs discovery only. It does **not** use
   `--scrape`/`--scrape-formats`.
2. Acquisition persists the authoritative search response and candidate
   identities, then schedules candidates for extraction.
3. Extraction creates an extraction-attempt record and performs one bounded
   `firecrawl scrape` operation per candidate.
4. URL suitability is checked before the child process is launched. Response
   status, source content type, usable markdown/text, and challenge/interstitial
   signatures are checked before blob writes or corpus ingestion.
5. Only suitable content is admitted to `CorpusService.ingest_batch()`.

This separation is restart-safe. If a process stops after discovery but before
candidate extraction completes, the persisted candidate identities can be
reconstructed and the provider operation can be rerun under the same bounded
policy. No historical extraction success or provenance is inferred.

## Deadline policy

The policy is explicit and configurable through environment variables:

| Variable | Default | Meaning |
|---|---:|---|
| `FIRECRAWL_EXTRACTION_FIRST_BYTE_TIMEOUT_SECONDS` | `10` | Maximum time before any provider stdout/stderr byte is observed. |
| `FIRECRAWL_EXTRACTION_PROVIDER_TIMEOUT_SECONDS` | `30` | Maximum duration of one candidate provider operation. |
| `FIRECRAWL_EXTRACTION_CANDIDATE_TIMEOUT_SECONDS` | `35` | Overall wall-clock bound across all attempts for one candidate. |
| `FIRECRAWL_EXTRACTION_TRANSIENT_RETRIES` | `2` | Additional attempts allowed for explicitly transient transport/HTTP failures. |
| `FIRECRAWL_EXTRACTION_EMPTY_RETRIES` | `0` | Additional attempts allowed for definitive empty content. |

All values fail closed on invalid configuration. A provider timeout terminates
the process group and waits for the provider process to exit before returning.
The timeout is classified as `timeout`, not `empty_content`.

## Failure and retry matrix

| Preflight outcome | Retry policy | Downstream action | Durable extraction failure class |
|---|---|---|---|
| `suitable` | none | Proceed to immutable blob/corpus ingestion | `none` on success |
| `empty_content` | `FIRECRAWL_EXTRACTION_EMPTY_RETRIES` (default 0) | Stop candidate work | `empty_content` |
| `anti_bot` | none | Stop candidate work | `anti_bot` |
| `unsupported_content_type` | none | Stop candidate work | `unsupported_format` |
| non-transient HTTP error | none | Stop candidate work | `http_error` |
| transient HTTP/transport failure | bounded by transient retry count and overall candidate deadline | Retry; fail after exhaustion | `network` |
| first-byte/provider/overall timeout | none | Terminate/reap provider work | `timeout` |
| malformed provider response | none | Fail closed | `malformed` |
| non-transient provider execution error | none | Fail closed | `internal` |

The in-memory preflight name `unsupported_content_type` intentionally maps to
the pre-existing PostgreSQL enum value `unsupported_format`. No schema change
is required and no incompatible enum value is written.

## Anti-bot detection

Challenge detection uses challenge-specific phrases and combinations such as
“verify you are human”, CAPTCHA instructions, or a Cloudflare/hCaptcha/
reCAPTCHA/Turnstile term near a challenge/verification/security marker. Generic
research prose containing terms such as “bot detection”, “Cloudflare”,
“interstitial”, or “turnstile” is not rejected merely because the term appears.

## Audit persistence and redaction

Terminal preflight outcomes create or complete a PostgreSQL
`extraction_attempts` record with:

- `exit_status` distinguishing cancellation from failure;
- the existing durable `failure_class` taxonomy;
- `backend_status` containing the preflight stage and reason code;
- `start_time`/`end_time` around the provider operation when available; and
- a bounded `error_message` containing failure class, reason code, failure
  stage, elapsed seconds, cancellation state, and sanitized reason text.

Diagnostic text passes through token/API-key/bearer redaction before
persistence. Rejected candidates do not write raw/normalized content blobs and
do not enter corpus ingestion.

## Compatibility and migration

No Alembic migration is required. Issue #216 reuses existing
`extraction_attempts` columns and the existing `extraction_failure_class` enum.
Timing and classification are carried in bounded transport/preflight metadata;
the public `SearchAdapterResult` domain contract is not expanded for this issue.
Persisted audit output uses existing PostgreSQL fields. Existing search-response
and immutable content hashing contracts are unchanged.

Rollback is therefore code-only: reverting the issue #216 provider/stage
routing restores the previous behavior without a database downgrade. Historical
rows are not backfilled or reclassified.

## Review remediation matrix

The final implementation supersedes the original inline/post-hoc preflight
attempt. The review findings are resolved at the production seam as follows:

| Finding | Final correction | Regression evidence |
|---|---|---|
| Suitable candidates were treated as rejected because `"suitable"` was truthy. | Admission is based on the typed outcome's `terminal` state; suitable outcomes remain non-terminal and continue to ingestion. | Mixed suitable/empty `BoundedExtractionStage` test asserts the suitable candidate succeeds while the empty candidate is cancelled. |
| Preflight ran after `search --scrape`, so it could not bound provider extraction. | Search is discovery-only and candidate `scrape` is a separate bounded provider operation. | Discovery-only command assertions plus candidate-scrape deadline tests. |
| Empty/whitespace markdown could bypass the checker. | Candidate scrape results always pass through `CandidatePreflightChecker` before blob/corpus admission; empty content is terminal by default. | Empty and whitespace suitability tests plus zero-empty-retry test. |
| Content type was not actually validated and the provisional failure name was incompatible with PostgreSQL. | MIME/content type is normalized and checked; `unsupported_content_type` maps to durable `unsupported_format`. | Unsupported MIME policy test and extraction-stage durable-enum test. |
| First-byte/provider/overall deadlines were post-hoc or absent and provider work could be orphaned. | `BoundedSubprocessRunner` enforces live deadlines, starts a separate process session, terminates the process group, escalates to `SIGKILL` when required, and waits for reaping. | Real child-process first-byte and provider-operation timeout tests. |
| Timeout provenance was conflated with empty content and timing was lost. | Timeout has its own classification/reason code/failure stage and elapsed timing is propagated in preflight metadata to the extraction audit record. | Timeout-classification test and PostgreSQL audit readback. |
| Provider diagnostics could persist credentials. | Provider errors, transport metadata, and audit messages pass through bearer/assignment redaction before persistence. | Transient-error redaction test and PostgreSQL audit readback containing `[REDACTED]`. |
| Anti-bot matching rejected legitimate research prose. | Detection requires challenge-specific phrases or provider/challenge term combinations rather than generic topical words. | Legitimate “bot detection”/Cloudflare article regression plus challenge rejection test. |
| Rejected candidates could contaminate corpus admission or ordinal handling. | Only non-terminal requests enter `active_requests`; existing `metadata.firecrawl.result_index` is preserved so `CorpusService.ingest_batch()` keeps original manifest ordinals after filtering. | Mixed-candidate stage test; existing `CorpusService` ordinal contract remains unchanged. |
| New tests did not exercise the responsible production seam and were absent from CI. | The issue suite imports the canonical package routing, executes `BoundedExtractionStage`, uses real child processes for cancellation, and performs PostgreSQL readback. A dedicated Python 3.11/3.12 workflow runs it with acquisition/orchestrator regressions. | `.github/workflows/extraction-preflight.yml`. |
| Superseded first-pass API and broad test-format churn remained in the diff. | `domain.py`, `acquisition_service.py`, and `orchestrator.py` are restored to the base implementation; the legacy acquisition test changes only the discovery-only command assertions. | Final PR diff inspection. |

No finding is resolved by weakening PostgreSQL authority, treating Qdrant as an
exact-membership source, inferring historical provenance, or allowing external
or provisional synthesis to satisfy an authoritative completion gate.

## Regression and CI coverage

`tests/integration/test_issue_216_extraction_preflight.py` covers:

- empty and whitespace markdown;
- anti-bot challenges and a legitimate “bot detection” article false-positive
  regression;
- unsupported content types;
- transient retry exhaustion/success;
- distinct timeout classes;
- first-byte process cancellation/reaping;
- provider-operation timeout after first byte;
- discovery-only search behavior;
- production `ExtractionStage` admission of suitable content while excluding
  rejected content;
- durable PostgreSQL audit readback, including enum compatibility, elapsed-stage
  text, cancellation, and secret redaction; and
- documentation/CI synchronization for the issue-specific contract.

The test file runs in a dedicated Python 3.11/3.12 issue #216 workflow with a
disposable PostgreSQL instance. The repository broad suite continues to run in
parallel, so issue-specific evidence cannot be hidden by unrelated green checks.

## Approved repository-tooling provenance

The maintainer expressly expanded PR #233's scope to include the Serena MCP
project configuration. `.serena/project.yml` is the shared project definition;
`.serena/.gitignore` and the root `.gitignore` exclude generated cache and local
override files. These files are repository-development tooling only: they do not
participate in research lifecycle authority, provider execution, PostgreSQL
completion decisions, content hashing, or Qdrant projection behavior.
