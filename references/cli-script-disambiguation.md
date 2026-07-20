# Firecrawl CLI Disambiguation

## Three Firecrawl Interfaces

| Interface | Package | Primary Use | Install Method |
|-----------|---------|-------------|----------------|
| **Node.js CLI** | `firecrawl-cli` (npm) | Scratch-file workflow (`fsearch`, `fscrape`, `fread`), search, scrape, crawl, map, parse | Install in the active Node/npm environment with `rtk proxy npm install -g firecrawl-cli` |
| **Python SDK** | `firecrawl-py` (PyPI) | Programmatic REST API calls from Python scripts | `pip install firecrawl-py` |
| **MCP Tools** | Firecrawl MCP server | Tool calls exposed by the host agent, such as search and scrape tools | Host-agent MCP configuration |

## Which Firecrawl Binary Do Scripts Use?

Launch `fsearch`/`fscrape`/`fread` through `rtk proxy`. Inside the wrappers, the scripts invoke `firecrawl` directly from `PATH` (no nested RTK, no `npx`, no `firecrawl-py`, no `python -m firecrawl`). This should resolve to the **Node.js CLI** installed for the current agent environment.

The Python SDK (`firecrawl-py` v4.28.2) is installed as a separate package and is **not** used by these scripts. The Python SDK is the Firecrawl REST API client — it's used when you want structured programmatic access (e.g. in an `execute_code` block), not for the scratch-file workflow.

## Key Differences

| Aspect | Node.js CLI | Python SDK | MCP Tools |
|--------|-------------|------------|-----------|
| Install | NVM/node global or npx | pip | MCP server config |
| Search | `firecrawl search "query"` | Client.search() | `firecrawl_search` tool |
| Scrape | `firecrawl scrape url` | Client.scrape_url() | `firecrawl_scrape` tool |
| Output | Console + file flags (`-o`) | Python objects | Tool output in agent context |
| Scratch files | Built-in via `fsearch`/`fscrape` scripts | Not built-in (you handle files) | Not built-in (you handle files) |
| Auth | `--api-key` or `FIRECRAWL_API_KEY` env | `FIRECRAWL_API_KEY` env | Inherited from MCP config |

## Troubleshooting `firecrawl` bin resolution

If `fsearch` returns exit 127 or "command not found":

1. Check whether `firecrawl` is on `PATH`: `rtk which firecrawl`
2. If it is absent, install the CLI into the active Node/npm environment: `rtk proxy npm install -g firecrawl-cli`
3. Verify the executable with `rtk proxy firecrawl --version`

## Environment Variables

All three interfaces share the same primary auth:

| Variable | Applies to |
|----------|------------|
| `FIRECRAWL_API_KEY` | Node CLI, Python SDK |
| `FIRECRAWL_API_URL` | Node CLI, Python SDK (custom base URL) |
| `FIRECRAWL_CATALOG_DIR` | Skill wrappers (persistent audit-catalog root) |
| `FIRECRAWL_CATALOG_DISABLED` | Skill wrappers (disable persistent catalog for private or isolated runs) |
| `FIRECRAWL_RESEARCH_RUN_ID` | Skill wrappers (explicit `fr_<uuid>` run linkage for multi-step research) |
| `FIRECRAWL_SEARCH_RETRIES` | `fsearch` (transient acquisition retry count; default `2`) |

The MCP tools get auth from the MCP server's own config.
