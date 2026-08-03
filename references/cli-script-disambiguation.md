# Firecrawl CLI Disambiguation

## Three Firecrawl interfaces

| Interface | Package | Use |
|---|---|---|
| Node.js CLI | `firecrawl-cli` | Provider transport invoked internally by authoritative `fsearch` and `fscrape` |
| Python SDK | `firecrawl-py` | Direct programmatic API work outside the bundled wrapper contract |
| MCP tools | Firecrawl MCP server | Host-agent tool calls; authoritative only when routed through the PostgreSQL acquisition services |

## Bundled wrapper contract

Launch bundled commands through `rtk proxy` at the outer agent-visible boundary. Inside the wrappers, `firecrawl` is invoked directly from `PATH` so streams and exit codes remain intact.

```bash
rtk proxy "<skill-root>/scripts/frun" start "Research objective"
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --research-run-id "fr_<uuid>"
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com" --research-run-id "fr_<uuid>"
```

Before the Node CLI or any network transport is constructed, the wrappers require:

- `DATABASE_URL`;
- Alembic head and writable PostgreSQL privileges;
- durable writable `BLOB_ROOT`;
- valid acquisition-eligible `fr_<uuid>` binding.

The wrappers return stable authoritative IDs. They do not use provider output files as runtime state.

## Interface differences

| Aspect | Node.js CLI | Python SDK | MCP tools |
|---|---|---|---|
| Installation | npm | pip | host-agent configuration |
| Authentication | `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL` | same API configuration | MCP server configuration |
| Bundled persistence | only through `fsearch` / `fscrape` authoritative services | caller must use an authoritative integration | tool output is not persisted unless routed through an authoritative integration |
| Result contract | wrapper returns run, invocation, response/candidate or corpus IDs | Python objects | tool result in agent context |
| Retry authority | PostgreSQL idempotency and invocation records | caller-defined | integration-defined |

Do not treat a direct SDK or MCP response as a successful Firecrawl Research Skill acquisition unless it is committed through the same PostgreSQL and `BLOB_ROOT` services.

## Binary resolution

If a bundled wrapper reports that `firecrawl` is unavailable:

```bash
rtk which firecrawl
rtk proxy npm install -g firecrawl-cli
rtk proxy firecrawl --version
```

Do not add `npx`, Python SDK substitution, MCP fallback, or a second transport inside the wrappers without an issue that preserves preflight, persistence, idempotency, and failure ordering.

## Environment variables

| Variable | Applies to |
|---|---|
| `FIRECRAWL_API_KEY` | Node CLI and Python SDK |
| `FIRECRAWL_API_URL` | Node CLI and Python SDK |
| `DATABASE_URL` | bundled authoritative services |
| `BLOB_ROOT` | immutable provider payload storage |
| `FIRECRAWL_RESEARCH_RUN_ID` | default wrapper run binding |
| `FIRECRAWL_INVOCATION_ID` | deliberate retry identity |
| `FIRECRAWL_SEARCH_RETRIES` | bounded transient `fsearch` transport retries |

Provider transport retries remain inside one authoritative invocation and operation budget. A failed authoritative preflight permits zero transport attempts.
