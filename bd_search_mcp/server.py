"""bd-search MCP server: Exa / Brave / Parallel search, each call from a new IP.

One tool per provider, so each provider subagent is given exactly one tool.
A tool call = build the provider request -> send it through a fresh Bright
Data exit IP -> normalise the results -> return them to the agent.

Tools never raise. Failures come back as
    {"provider", "error", "message", "retryable", "attempts"}
so the subagent can decide whether to retry or report.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
from functools import lru_cache
from typing import Annotated, Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from bd_search_mcp import providers
from bd_search_mcp.config import get_settings
from bd_search_mcp.egress import EgressError, RotatingEgress
from bd_search_mcp.providers import ProviderRequest

logger = logging.getLogger("bd_search_mcp")

MAX_QUERY_CHARS = 2000
MAX_RESULTS = 25
MAX_DOMAINS = 20
MAX_PARALLEL_QUERIES = 5
DEFAULT_RESULTS = 10
# Egress failures another attempt may fix (the next call gets a new IP / slot).
RETRYABLE_EGRESS = frozenset({"busy", "proxy_error", "timeout", "upstream_error"})
HEALTH_PATH = "/healthz"

Query = Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
NumResults = Annotated[int, Field(ge=1, le=MAX_RESULTS)]
Domains = Annotated[list[Annotated[str, Field(max_length=253)]], Field(max_length=MAX_DOMAINS)]
IsoDate = Annotated[str, Field(pattern=r"^(\d{4}-\d{2}-\d{2}.*)?$", max_length=40)]

# host="0.0.0.0" also keeps FastMCP's localhost-only Host-header check off, which
# would otherwise refuse calls arriving as http://bd-search:8080 inside a cluster.
mcp = FastMCP("bd-search", host="0.0.0.0", stateless_http=True, json_response=True)  # noqa: S104  # nosec B104 - container listens on all interfaces


@lru_cache
def get_egress() -> RotatingEgress:
    s = get_settings()
    return RotatingEgress(s, allowed_hosts=s.provider_hosts)


def _error(provider: str, code: str, message: str, *, retryable: bool, attempts: int = 0) -> dict[str, Any]:
    return {"provider": provider, "error": code, "message": message, "retryable": retryable, "attempts": attempts}


async def _call(provider: str, api_key: str, build: Any, parse: Any) -> dict[str, Any]:
    if not api_key:
        return _error(provider, "not_configured", f"{provider.upper()}_API_KEY is not set", retryable=False)
    req: ProviderRequest = build(api_key)
    try:
        resp = await get_egress().send(
            req.method, req.url, headers=req.headers, params=req.params, json_body=req.json_body
        )
    except EgressError as exc:
        logger.warning("tool.failed provider=%s error=%s attempts=%d", provider, exc.code, exc.attempts)
        return _error(provider, exc.code, exc.message, retryable=exc.code in RETRYABLE_EGRESS, attempts=exc.attempts)

    egress = dataclasses.asdict(resp.egress)
    if resp.status >= 400:
        failure = providers.classify_status(resp.status, resp.text(providers.ERROR_DETAIL_CHARS))
        logger.warning("tool.failed provider=%s error=%s status=%d", provider, failure.code, resp.status)
        out = _error(
            provider,
            failure.code,
            f"HTTP {resp.status}: {failure.detail}",
            retryable=failure.retryable,
            attempts=resp.egress.attempts,
        )
        return {**out, "status": resp.status, "egress": egress}
    try:
        results = parse(resp.json())
    except ValueError:
        attempts = resp.egress.attempts
        return _error(provider, "bad_response", "provider returned non-JSON", retryable=True, attempts=attempts)
    logger.info(
        "tool.ok provider=%s results=%d attempts=%d latency_ms=%d",
        provider,
        len(results),
        resp.egress.attempts,
        resp.egress.latency_ms,
    )
    return {"provider": provider, "results": results, "egress": egress}


@mcp.tool()
async def exa_search(
    query: Query,
    num_results: NumResults = DEFAULT_RESULTS,
    include_domains: Domains | None = None,
    exclude_domains: Domains | None = None,
    start_published_date: IsoDate = "",
    end_published_date: IsoDate = "",
) -> dict[str, Any]:
    """Search the web with Exa (neural search, good for research and long-tail topics).

    Returns {"provider", "results": [{"title", "url", "snippets", "published_date"?}], "egress"},
    or {"provider", "error", "message", "retryable", "attempts"} on failure.

    Args:
        query: What to search for, in natural language.
        num_results: Number of results, 1-25.
        include_domains: Only return results from these domains, e.g. ["rbi.org.in"].
        exclude_domains: Never return results from these domains.
        start_published_date: ISO date (YYYY-MM-DD); only pages published after it.
        end_published_date: ISO date (YYYY-MM-DD); only pages published before it.
    """
    s = get_settings()
    return await _call(
        "exa",
        s.exa_api_key.get_secret_value(),
        lambda key: providers.exa_request(
            s.exa_search_url,
            key,
            query=query,
            num_results=num_results,
            include_domains=include_domains or [],
            exclude_domains=exclude_domains or [],
            start_published_date=start_published_date,
            end_published_date=end_published_date,
        ),
        providers.parse_exa,
    )


@mcp.tool()
async def brave_search(
    query: Query,
    num_results: Annotated[int, Field(ge=1, le=providers.BRAVE_MAX_COUNT)] = DEFAULT_RESULTS,
    country: Annotated[str, Field(pattern=r"^([A-Za-z]{2})?$")] = "",
    freshness: Annotated[str, Field(pattern=r"^(pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})?$")] = "",
) -> dict[str, Any]:
    """Search the web with Brave (keyword web index, good for news and current facts).

    Returns {"provider", "results": [{"title", "url", "snippets", "published_date"?}], "egress"},
    or {"provider", "error", "message", "retryable", "attempts"} on failure.

    Args:
        query: Search keywords. Supports operators like site:example.com.
        num_results: Number of results, 1-20.
        country: Two-letter country to bias results to, e.g. "IN". Empty = any.
        freshness: pd (past day), pw (week), pm (month), py (year), or YYYY-MM-DDtoYYYY-MM-DD.
    """
    s = get_settings()
    return await _call(
        "brave",
        s.brave_api_key.get_secret_value(),
        lambda key: providers.brave_request(
            s.brave_search_url, key, query=query, num_results=num_results, country=country, freshness=freshness
        ),
        providers.parse_brave,
    )


@mcp.tool()
async def parallel_search(
    objective: Query,
    search_queries: Annotated[list[Query], Field(max_length=MAX_PARALLEL_QUERIES)] | None = None,
    num_results: NumResults = DEFAULT_RESULTS,
) -> dict[str, Any]:
    """Search the web with Parallel (objective-driven, returns LLM-ready excerpts).

    Returns {"provider", "results": [{"title", "url", "snippets", "published_date"?}], "egress"},
    or {"provider", "error", "message", "retryable", "attempts"} on failure.

    Args:
        objective: What you are trying to find out, in one or two sentences.
        search_queries: Optional keyword queries (up to 5) to guide the search.
        num_results: Number of results, 1-25.
    """
    s = get_settings()
    return await _call(
        "parallel",
        s.parallel_api_key.get_secret_value(),
        lambda key: providers.parallel_request(
            s.parallel_search_url,
            key,
            objective=objective,
            search_queries=search_queries or [],
            num_results=num_results,
        ),
        providers.parse_parallel,
    )


@mcp.custom_route(HEALTH_PATH, methods=["GET"])
async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
    s = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "proxy_configured": s.proxy_configured,
            "require_proxy": s.require_proxy,
            "providers": {
                "exa": bool(s.exa_api_key.get_secret_value()),
                "brave": bool(s.brave_api_key.get_secret_value()),
                "parallel": bool(s.parallel_api_key.get_secret_value()),
            },
        }
    )


class BearerAuth:
    """Pure ASGI (not BaseHTTPMiddleware) so MCP streaming responses pass through untouched."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH:
            sent = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(sent, self._expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


def build_app() -> ASGIApp:
    app: ASGIApp = mcp.streamable_http_app()
    token = get_settings().auth_token.get_secret_value()
    return BearerAuth(app, token) if token else app


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if s.require_proxy and not s.proxy_configured:
        logger.error("startup: Bright Data zone not configured; every tool call will answer proxy_not_configured")
    if not s.auth_token.get_secret_value():
        logger.warning("startup: AUTH_TOKEN is empty; /mcp is open to anyone who can reach this port")
    uvicorn.run(build_app(), host=s.bind_host, port=s.port, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()
