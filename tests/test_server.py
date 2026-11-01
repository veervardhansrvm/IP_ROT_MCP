from __future__ import annotations

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from bd_search_mcp import server
from bd_search_mcp.config import Settings
from bd_search_mcp.egress import RotatingEgress

pytestmark = pytest.mark.anyio


@pytest.fixture
def wire(monkeypatch):
    """Point the server at fake settings + a MockTransport upstream; returns the seen requests."""
    seen: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return responses.pop(0)

    def factory(proxy, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    settings = Settings(
        brightdata_customer_id="hl_test",
        brightdata_zone="resi",
        brightdata_password="pw",
        brave_api_key="brave-key",
        exa_api_key="",
        _env_file=None,
    )
    egress = RotatingEgress(settings, settings.provider_hosts, client_factory=factory)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "get_egress", lambda: egress)
    return seen, responses


async def _call(name: str, args: dict) -> dict:
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(name, args)
    return json.loads(result.content[0].text)


async def test_lists_one_tool_per_provider():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {"exa_search", "brave_search", "parallel_search"}


async def test_brave_search_end_to_end(wire):
    seen, responses = wire
    hit = {"title": "T", "url": "https://x", "description": "d"}
    responses.append(httpx.Response(200, json={"web": {"results": [hit]}}))
    out = await _call("brave_search", {"query": "rbi repo rate", "num_results": 3})
    assert out["provider"] == "brave"
    assert out["results"] == [{"title": "T", "url": "https://x", "snippets": ["d"]}]
    assert out["egress"]["proxy"] == "brightdata"
    assert seen[0].headers["X-Subscription-Token"] == "brave-key"
    assert seen[0].url.params["q"] == "rbi repo rate"


async def test_provider_429_comes_back_as_retryable_error(wire):
    _, responses = wire
    responses.append(httpx.Response(429, text="too many"))
    out = await _call("brave_search", {"query": "q"})
    assert out["error"] == "rate_limited"
    assert out["retryable"] is True
    assert out["status"] == 429


async def test_missing_key_is_not_configured(wire):
    seen, _ = wire
    out = await _call("exa_search", {"query": "q"})
    assert out["error"] == "not_configured"
    assert seen == []


async def test_bearer_auth_guards_mcp_but_not_health():
    app = server.BearerAuth(server.mcp.streamable_http_app(), "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.post("/mcp", json={})).status_code == 401
        assert (await client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await client.get("/healthz")).status_code == 200
