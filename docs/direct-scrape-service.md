# PostgreSQL-authoritative direct scrape service

`research_store.DirectScrapeService` is the service-layer replacement for
file-mediated scrape ingestion. It accepts direct URLs or run-scoped stable
candidate IDs. It never accepts a source path, scratch directory, or ingestion
manifest.

## Authority boundary

Before the Firecrawl adapter is constructed, the service requires the shared
authoritative-acquisition preflight and verifies the additional PostgreSQL
privileges used by direct scrape persistence. PostgreSQL owns run, invocation,
candidate, extraction-attempt, provenance, corpus, derivation, and index-job
state. Raw response bytes are immutable content-addressed objects under
`BLOB_ROOT`. Qdrant is rebuilt from PostgreSQL index jobs; Valkey notification
is optional.

Each invocation item is serialized with a PostgreSQL session advisory lock.
Concurrent callers using the same invocation idempotency key therefore observe
one provider execution and one committed item result. A lost process releases
the lock when its PostgreSQL connection closes; a later caller revalidates the
run and resumes the uncommitted item.

## Format and MIME contract

The canonical formats are `markdown`, `html`, `rawHtml`, `json`, `links`,
`images`, and `summary`. Schema extraction canonicalizes to `json`.
`summary=True` canonicalizes to the `summary` format rather than combining a
different format with a shortcut flag. MIME overrides must agree with the
canonical format.

The Firecrawl CLI adapter requests one format and treats stdout as the raw
single-format payload. It records the CLI version when available, a sanitized
command, request options, timings, exit status, bounded diagnostics, and
provider metadata in an append-only event associated with the extraction
attempt. Payload bytes are never embedded in invocation or event JSON.

## Replay and retry

The same invocation idempotency key is strict replay/resume: committed items
are returned without another provider call, while an interrupted running
invocation continues only its uncommitted items. `retry_failed()` is an
explicit new invocation with a new idempotency key. It retries only failed
items and records both `parent_invocation_id` and extraction-attempt
`retry_parent_id`.

## Parser and derivation identity

Corpus preparation returns a named `PreparedIngest` contract containing the
actual parser type and implementation version selected from the MIME-aware
registry while preserving the established configured parser-version contract.
The optional `parser_name` persistence argument is additive and trailing, so
legacy positional callers retain their existing behavior. Document reuse
includes the exact parser identity. Identical source bytes may reuse an
immutable snapshot, but JSON, HTML, Markdown, and plain-text parsing cannot
silently reuse one another's documents, chunks, or derivations.

## Validation boundary

Focused CI uses deterministic transport adapters and disposable PostgreSQL to
validate command construction, every supported format and MIME contract,
preflight ordering, parser-aware persistence, concurrent idempotency, retry
lineage, crash recovery, immutable provenance, and index-job creation. It does
not make a live Firecrawl network request. Live provider validation remains an
exact-candidate release-gate responsibility under parent epic #183 rather than
a prerequisite for deterministic pull-request tests.

At the final reviewed head, the focused direct-scrape suite passes 20 tests on
both Python 3.11 and 3.12, and the repository suite passes 1,472 tests with four
existing skips on both versions. Ruff, acquisition-authority, release-invariant,
strict-campaign, and research-environment checks also pass on the same head.
