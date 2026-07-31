# Reduced real smoke gate

The reduced smoke gate is diagnostic evidence only. It runs exactly two strict campaigns and one benchmark objective. By default, each repetition runs `autonomous_local` and `deterministic_debug`. The `agent_led` mode is optional.

The gate remains fail-closed: both recommendations must be `go` and the production reproducibility comparison must pass for the selected execution-mode set.

## Default two-mode run

No host-artifact supplier is required for the default run:

```bash
python scripts/smoke_test.py \
  --candidate-sha "$(git rev-parse HEAD)" \
  --objective obj-001 \
  --database-url "$DATABASE_URL" \
  --qdrant-url "$QDRANT_URL"
```

The effective modes are:

- `autonomous_local`
- `deterministic_debug`

## Optional agent-led run

Add `--include-agent-led` to run all three modes. Only this form requires an external host-artifact supplier:

```bash
export GENERATIVE_URL='http://127.0.0.1:8002/v1'
export SMOKE_HOST_SUPPLIER_COMMAND='/path/to/external-agent --stdio'
export SMOKE_HOST_SUPPLIER_IDENTITY='external-review-agent'
export SMOKE_HOST_SUPPLIER_ENDPOINT='http://external-review-agent:9000'

python scripts/smoke_test.py \
  --candidate-sha "$(git rev-parse HEAD)" \
  --objective obj-001 \
  --database-url "$DATABASE_URL" \
  --qdrant-url "$QDRANT_URL" \
  --include-agent-led
```

`agent_led` must use a process distinct from the autonomous local-model endpoint. Both authorities must expose explicit endpoint fingerprints. `SMOKE_HOST_SUPPLIER_ENDPOINT` must not resolve to the configured autonomous endpoint, and the supplier must not proxy that endpoint under another label.

The external command receives one JSON document on stdin and returns one JSON document on stdout. Its probe and supply protocol remains `firecrawl-host-artifact-stdio-v1`.

## Administrative environment override

A truthy `SMOKE_DISABLE_AGENT_LED` always disables `agent_led`, even when `--include-agent-led` is present:

```bash
export SMOKE_DISABLE_AGENT_LED=1
```

Accepted true values are `1`, `true`, `yes`, and `on`; accepted false values are `0`, `false`, `no`, and `off`, case-insensitively. Any other value fails closed.

For example, this still runs only the default two modes:

```bash
SMOKE_DISABLE_AGENT_LED=1 python scripts/smoke_test.py \
  --candidate-sha "$(git rev-parse HEAD)" \
  --objective obj-001 \
  --database-url "$DATABASE_URL" \
  --qdrant-url "$QDRANT_URL" \
  --include-agent-led
```

The manifest records whether `agent_led` was requested, disabled by the environment, and effectively selected. When it is not selected, `host_supplier` is `null`.

## Mandatory run checks

Commit all source changes first. The gate rejects a dirty checkout and any candidate SHA that differs from `HEAD`. The complete real-stack preflight must pass before either campaign starts.

For every selected exact run, the gate requires:

- the expected persisted semantic authority and no competing authority;
- substantive source candidates, run assets, snapshots, documents, and chunks;
- claims, claim-evidence links, evidence packets, and completed semantic calls;
- completed `outline`, `binding`, `draft`, `citation_pass`, and `validation` stages;
- at least 200 characters of report body from completed `draft.report_sections`;
- all mandatory metric statuses and values required by strict release policy;
- passing completeness and integrity checks.

Artifacts are written below `/tmp/firecrawl_smoke_test/<candidate-sha>/` by default. The manifest records the selected mode set, exact SHA and tree identity, service fingerprints, optional supplier provenance, full result serialization, exact-run evidence counts, semantic authorities, draft report hashes, and the production reproducibility comparison.

Do not start the corresponding full campaign unless the manifest states:

```json
{"gate":"PASS","full_campaign_authorized":true}
```
