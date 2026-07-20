# Catalog v5 Reference

## Contents

1. Data model
2. Research manifests
3. Staged LLM audits
4. Provider and budget controls
5. Maintenance and schema reset

## Data model

Catalog v5 separates immutable observations, bounded evidence, append-only LLM
assessments, and run rollups. Programmatic code records mechanical facts only;
it does not decide relevance, authority, freshness, claim support, research
adequacy, or outcome consistency.

- `operational_status` describes process execution.
- `data_completeness` describes whether expected audit inputs are present.
- `audit_status` describes the staged LLM audit lifecycle.
- `assessment_summary` contains the current model-assessed result and provenance.
- `operational_metrics` and `operational_summary` contain counts, durations, and
  extraction outcomes without semantic labels.

Each retained result has a stable `candidate_id`, exact constraint status,
nonbinding source hints, all collected publication/update date signals, and
bounded excerpts. Each excerpt has a stable `excerpt_id`, source offset, text
hash, selection reason, and bounded text. Compressed snapshots preserve this
evidence if temporary scratch files expire.

## Research manifests

Prefer exact candidate and excerpt references:

```json
{
  "claims": [
    {"id": "claim-1", "summary": "Short claim summary", "type": "finding"}
  ],
  "sources": [
    {
      "url": "https://example.gov/primary-source",
      "candidate_id": "fce_0123456789abcdef01234567",
      "claim_ids": ["claim-1"],
      "excerpt_ids": ["fex_0123456789abcdef01234567"],
      "roles": ["primary"],
      "relationship": "supports"
    }
  ]
}
```

Claim IDs must be unique. Candidate and excerpt references must resolve to the
same recorded source. URL-only entries remain supported but are lower fidelity
and fail when a URL is ambiguous.

Use `frun finish ... --answer-file final.md` to preserve the delivered answer,
its hash, and the exact object the audit should compare with the manifest.

## Staged LLM audits

Every completed explicit research run is audited locally unless
`FIRECRAWL_AUDIT_AUTO_SEMANTIC=0` is set. Standalone invocations are assessed
only on demand. Audits run four stages:

1. Build an objective-specific rubric.
2. Assess acquisition, strategy, failures, pivots, and efficiency.
3. Assess sources, dates, claim support, independence, and excerpts.
4. Synthesize adequacy, outcome consistency, and prioritized recommendations.

All findings must cite stored run, invocation, candidate, claim, excerpt, or
event IDs. Unknown references fail validation. Scraped text is delimited as
untrusted evidence. Missing metadata produces uncertainty rather than an
automatic negative judgment.

```bash
frun audit fr_<uuid> --llm local
frun audit fr_<uuid> --stages acquisition,evidence,synthesis
frun audit fr_<uuid> --max-calls 8 --max-input-tokens 48000
frun audit-status fr_<uuid>
frun aggregate
```

## Provider and budget controls

The local default is `http://192.168.4.115:8002/v1`, model `chat`. Override it
with `FIRECRAWL_LLM_LOCAL_BASE_URL` and `FIRECRAWL_LLM_LOCAL_MODEL`; legacy
`FIRECRAWL_AUDIT_LOCAL_*` variables remain accepted.

Commercial OpenAI or Gemini processing requires explicit provider, model, and
credentials. An explicit local-first fallback requires both
`--commercial-fallback openai|gemini` and `--fallback-model MODEL`; no automatic
path sends research data to a paid provider.

The provider gateway retains API surface, request and model IDs, finish/refusal
state, usage including reasoning tokens when available, bounded response
excerpts, schema results, latency, retries, and fallback lineage. Empty content
with reasoning or length exhaustion increases the output budget once before a
JSON-object repair attempt.

Default automatic limits are 48k estimated input tokens per call, 16,384 output
tokens, eight calls, 120 seconds per call, and eight minutes total. Candidate
ledgers split on token boundaries; every used source, claim, excerpt, and final
answer remains in the evidence stage.

## Maintenance and schema reset

```bash
frun recompute <fc_or_fr_id>
frun compare <fr_id> <fr_id>
frun migrate --from 4 --to 5
frun migrate --from 4 --to 5 --apply
frun purge --before 2026-07-01T00:00:00Z
frun purge --keep-last 10
frun purge --orphans
```

Migration is a dry run unless `--apply` is supplied. Applying the v5 transition
discards the complete old catalog without conversion or backup, then creates a
new schema marker. Purge is likewise a dry run unless `--force` is supplied.

Collection limits and LLM budgets live in `catalog-policy-v5.json`. Change the
policy, evaluator, and prompt-template versions whenever their behavior changes.
