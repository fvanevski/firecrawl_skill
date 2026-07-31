# Reduced real smoke gate

The reduced smoke gate is diagnostic evidence only. It runs exactly two strict campaigns, each containing all three execution modes and one benchmark objective. The full campaign remains blocked unless both recommendations are `go` and the production reproducibility comparison passes.

## External host-artifact protocol

`agent_led` must use a process that is distinct from the autonomous local-model endpoint. Configure:

```bash
export SMOKE_HOST_SUPPLIER_COMMAND='/path/to/external-agent --stdio'
export SMOKE_HOST_SUPPLIER_IDENTITY='external-review-agent'
export SMOKE_HOST_SUPPLIER_ENDPOINT='http://external-review-agent:9000'
```

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

Artifacts are written below `/tmp/firecrawl_smoke_test/<candidate-sha>/` by default. The manifest records exact SHA and tree identity, service and supplier fingerprints, full result serialization, exact-run evidence counts, semantic authorities, report hashes, and the production reproducibility comparison.

Do not start the full campaign unless the manifest states:

```json
{"gate":"PASS","full_campaign_authorized":true}
```
