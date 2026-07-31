# Reduced real smoke gate

The reduced smoke gate is diagnostic evidence only. It runs exactly two strict campaigns, each containing all three execution modes and one benchmark objective. The full campaign remains blocked unless both recommendations are `go` and the production reproducibility comparison passes.

## External host-artifact protocol

`agent_led` must use a process that is distinct from the autonomous local-model endpoint. Both authorities must expose explicit endpoint fingerprints; the gate fails closed when the autonomous endpoint cannot be resolved from `GENERATIVE_URL`, `FIRECRAWL_LLM_LOCAL_BASE_URL`, or `FIRECRAWL_AUDIT_LOCAL_BASE_URL`.

Configure the autonomous endpoint and the external host supplier:

```bash
export GENERATIVE_URL='http://127.0.0.1:8002/v1'
export SMOKE_HOST_SUPPLIER_COMMAND='/path/to/external-agent --stdio'
export SMOKE_HOST_SUPPLIER_IDENTITY='external-review-agent'
export SMOKE_HOST_SUPPLIER_ENDPOINT='http://external-review-agent:9000'
```

`SMOKE_HOST_SUPPLIER_ENDPOINT` must not resolve to the configured autonomous endpoint. The external supplier command must perform the host-authored semantic work itself; it must not proxy the autonomous model endpoint under a different label.

The command receives one JSON document on stdin and returns one JSON document on stdout.

Probe request:

```json
{"protocol":"firecrawl-host-artifact-stdio-v1","operation":"probe","supplier_identity":"external-review-agent"}
```

Required probe response:

```json
{"status":"available","supplier_identity":"external-review-agent","source_endpoint":"http://external-review-agent:9000"}
```

Supply requests contain `semantic_context`, the JSON `schema`, prompts, provider, model, and prompt version. The response must contain an object-valued `value`; optional `provenance` and `attempts` fields are preserved. The smoke harness adds request, artifact, and command hashes and rejects a source endpoint equal to the autonomous model endpoint.

## Run

Commit all source changes first. The gate rejects a dirty checkout and any candidate SHA that differs from `HEAD`.

```bash
python scripts/smoke_test.py \
  --candidate-sha "$(git rev-parse HEAD)" \
  --objective obj-001 \
  --database-url "$DATABASE_URL" \
  --qdrant-url "$QDRANT_URL"
```

The complete real-stack preflight must pass before either campaign starts. The gate then requires, for every exact run:

- the expected persisted semantic authority and no competing authority;
- substantive source candidates, run assets, snapshots, documents, and chunks;
- claims, claim-evidence links, evidence packets, and completed semantic calls;
- completed `outline`, `binding`, `draft`, `citation_pass`, and `validation` stages;
- at least 200 characters of report body from completed `draft.report_sections`;
- all mandatory metric statuses and values required by strict release policy;
- passing completeness and integrity checks.

Artifacts are written below `/tmp/firecrawl_smoke_test/<candidate-sha>/` by default. The manifest records exact SHA and tree identity, service and supplier fingerprints, full result serialization, exact-run evidence counts, semantic authorities, draft report hashes, and the production reproducibility comparison.

Do not start the full campaign unless the manifest states:

```json
{"gate":"PASS","full_campaign_authorized":true}
```
