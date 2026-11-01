# bd-search-mcp

A standalone MCP server that runs web searches on **Exa, Brave and Parallel**.
Every call leaves from a **new Bright Data exit IP**, and the server returns
normalised results to the agent. It has no Panja code in it: Panja (or any
MCP client) connects to it over HTTP.

```
Panja main agent
   └─ provider subagent (one per provider, one tool each)
        │  MCP tools/call  (streamable HTTP, POST /mcp, Bearer AUTH_TOKEN)
        ▼
bd-search-mcp
   1. validate args            (bad args -> MCP error, never sent out)
   2. build provider request   (API key added here; the agent never sees keys)
   3. new Bright Data session  brd-customer-<id>-zone-<zone>-session-<random>
        │  HTTPS CONNECT tunnel, TLS end to end
        ▼
   Bright Data super-proxy  ->  new residential exit IP
        ▼
   api.exa.ai | api.search.brave.com | api.parallel.ai
        ▼
   4. normalise results -> tool result back to the subagent
```

## Tools

| Tool | Arguments |
|---|---|
| `exa_search` | `query` (required), `num_results` 1–25, `include_domains`, `exclude_domains`, `start_published_date`, `end_published_date` (YYYY-MM-DD) |
| `brave_search` | `query` (required), `num_results` 1–20, `country` (e.g. `IN`), `freshness` (`pd`/`pw`/`pm`/`py` or `YYYY-MM-DDtoYYYY-MM-DD`) |
| `parallel_search` | `objective` (required), `search_queries` (up to 5), `num_results` 1–25 |

**Success** (all three return the same shape):
```json
{
  "provider": "brave",
  "results": [{"title": "...", "url": "https://...", "snippets": ["..."], "published_date": "..."}],
  "egress": {"proxy": "brightdata", "exit_session": "9f2c...", "attempts": 1, "latency_ms": 1240}
}
```

**Failure.** Tools never throw for runtime failures; they return this:
```json
{"provider": "brave", "error": "rate_limited", "message": "HTTP 429: ...", "retryable": true, "attempts": 1}
```

| `error` | Meaning | `retryable` |
|---|---|---|
| `not_configured` | that provider's API key is not set | no |
| `proxy_not_configured` | no Bright Data zone and `REQUIRE_PROXY=true` | no |
| `auth_failed` / `quota_exhausted` | provider said 401/403 / 402 | no |
| `bad_request` | provider said 400/404/413/422 | no |
| `rate_limited` | provider said 429 | yes |
| `provider_error` | provider said 5xx (retryable) or another status | 5xx only |
| `proxy_error` | every attempt failed to connect through Bright Data | yes |
| `timeout` | budget (`TIMEOUT_SECONDS`, default 30 s) spent | yes |
| `busy` | more than `MAX_CONCURRENT_REQUESTS` calls in flight | yes |
| `upstream_error` / `bad_response` / `response_too_large` | transport error / non-JSON / over 5 MiB | yes / yes / no |

Arguments that break the schema (an empty query, `num_results` of 500) come
back as an MCP error (`isError: true`) without calling anyone.

**Retry rule (inside the server):** only failures that happen *before* the
request reaches the provider are retried: proxy refused, tunnel failed, or
connect failed. Each retry uses a new exit IP, up to `MAX_ATTEMPTS` (default 3).
Anything after that, such as a provider 429 or 5xx, is returned once with
`retryable`. That way a billed POST is never sent twice, and the subagent
decides whether to try again or switch provider.

## Run locally

```powershell
cd C:\Users\veer1\Downloads\BD_egress\bd_search_mcp
copy .env.example .env      # fill Bright Data zone + provider keys + AUTH_TOKEN
.\run.ps1 test              # unit tests, no network
.\run.ps1 smoke             # live: 10 IP-echo calls (expect ~10 IPs) + each tool
.\run.ps1                   # serve on http://localhost:8080/mcp
```

Docker:
```powershell
docker build -t bd-search-mcp .
docker run --rm -p 8080:8080 --env-file .env bd-search-mcp
```

`GET /healthz` reports whether the proxy and each key are configured (never the values).

## Connect Panja

Register it as a remote MCP server:
- URL: `http://<host>:8080/mcp` (streamable HTTP, stateless)
- Auth: static header `Authorization: Bearer <AUTH_TOKEN>`

Give each provider subagent only its own tool (`exa_search`, `brave_search`
or `parallel_search`).

## Configuration (env or .env)

| Variable | Default | |
|---|---|---|
| `BRIGHTDATA_CUSTOMER_ID` / `_ZONE` / `_PASSWORD` | — | residential (or datacenter) zone; **not** Web Unlocker / SERP |
| `BRIGHTDATA_HOST` / `_PORT` | `brd.superproxy.io` / `33335` | older accounts use port 22225 |
| `BRIGHTDATA_COUNTRY` | any | e.g. `in` |
| `REQUIRE_PROXY` | `true` | `false` = go out directly when no zone (dev only) |
| `EXA_API_KEY` / `BRAVE_API_KEY` / `PARALLEL_API_KEY` | — | |
| `EXA_SEARCH_URL` / `BRAVE_SEARCH_URL` / `PARALLEL_SEARCH_URL` | provider defaults | these hosts are also the only reachable hosts |
| `AUTH_TOKEN` | empty (open) | set it anywhere but your laptop |
| `PORT` | `8080` | |
| `MAX_ATTEMPTS` | `3` | connect/proxy retries, new IP each |
| `TIMEOUT_SECONDS` / `CONNECT_TIMEOUT_SECONDS` | `30` / `10` | |
| `MAX_RESPONSE_BYTES` | 5 MiB | |
| `MAX_CONCURRENT_REQUESTS` / `CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS` | `64` / `5` | |

## Security notes

- Provider keys and the zone password exist only in this server's env. They are
  never logged and never returned to the agent.
- The server is not an open proxy. It can reach only the three provider hosts
  over HTTPS on port 443.
- TLS runs end to end through the CONNECT tunnel, so Bright Data cannot read
  queries, keys or results.
- Logs carry the provider, outcome, attempt count and latency. They never carry
  queries, keys or result bodies.

## Layout

```
bd_search_mcp/config.py      settings (only place env is read)
bd_search_mcp/brightdata.py  proxy URL + session ids
bd_search_mcp/egress.py      send through a fresh exit IP: allowlist, retries, caps, concurrency
bd_search_mcp/providers.py   per-provider request builders + result parsers (pure)
bd_search_mcp/server.py      MCP tools, /healthz, bearer auth, entrypoint
tests/                       unit tests (MockTransport + in-memory MCP client)
smoke.py                     live check against your zone and keys
```
