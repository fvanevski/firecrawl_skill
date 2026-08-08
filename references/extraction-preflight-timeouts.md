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
The added policy metadata is in-memory/diagnostic data; persisted audit output
uses existing PostgreSQL fields. Existing search-response and immutable content
hashing contracts are unchanged.

Rollback is therefore code-only: reverting the issue #216 provider/stage
routing restores the previous behavior without a database downgrade. Historical
rows are not backfilled or reclassified.

## Regression and CI coverage

`scripts/test_issue_216_extraction_preflight.py` covers:

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
  text, cancellation, and secret redaction.

The test file runs in a dedicated Python 3.11/3.12 issue #216 workflow with a
disposable PostgreSQL instance. The repository broad suite continues to run in
parallel, so issue-specific evidence cannot be hidden by unrelated green checks.
