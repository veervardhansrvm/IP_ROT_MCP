from __future__ import annotations

from urllib.parse import unquote, urlsplit

import httpx
import pytest

from bd_search_mcp.brightdata import proxy_url
from bd_search_mcp.config import Settings
from bd_search_mcp.egress import EgressError, RotatingEgress

pytestmark = pytest.mark.anyio

HOST = "api.search.brave.com"
URL = f"https://{HOST}/res/v1/web/search"


def _settings(**kw) -> Settings:
    base = dict(
        brightdata_customer_id="hl_test",
        brightdata_zone="resi",
        brightdata_password="p@ss/word",
        max_attempts=3,
        _env_file=None,
    )
    return Settings(**{**base, **kw})


class FakeUpstream:
    """Client factory over MockTransport; records the proxy URL of every attempt."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.proxies: list[str | None] = []

    def __call__(self, proxy, timeout) -> httpx.AsyncClient:
        self.proxies.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=timeout)

    def sessions(self) -> list[str]:
        return [unquote(urlsplit(p).username or "").rsplit("-session-", 1)[1] for p in self.proxies if p]


def _egress(fake: FakeUpstream, **kw) -> RotatingEgress:
    return RotatingEgress(_settings(**kw), frozenset({HOST}), client_factory=fake)


async def test_success_goes_through_proxy_with_one_attempt():
    fake = FakeUpstream(lambda req: httpx.Response(200, json={"ok": True}))
    resp = await _egress(fake).send("GET", URL, params={"q": "x"})
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert resp.egress.proxy == "brightdata"
    assert resp.egress.attempts == 1
    assert len(fake.proxies) == 1


async def test_connect_failures_retry_each_on_a_new_session():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ProxyError("407", request=req)
        return httpx.Response(200, json={})

    fake = FakeUpstream(handler)
    resp = await _egress(fake).send("GET", URL)
    assert resp.egress.attempts == 3
    assert len(set(fake.sessions())) == 3
    assert resp.egress.exit_session == fake.sessions()[-1]


async def test_all_attempts_failing_is_proxy_error():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(EgressError) as exc:
        await _egress(FakeUpstream(handler)).send("GET", URL)
    assert exc.value.code == "proxy_error"
    assert exc.value.attempts == 3


async def test_read_timeout_is_not_retried():
    fake = FakeUpstream(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)))
    with pytest.raises(EgressError) as exc:
        await _egress(fake).send("POST", URL, json_body={"q": 1})
    assert exc.value.code == "timeout"
    assert len(fake.proxies) == 1


async def test_provider_error_status_is_returned_not_raised():
    fake = FakeUpstream(lambda req: httpx.Response(429, text="slow down"))
    resp = await _egress(fake).send("GET", URL)
    assert resp.status == 429
    assert len(fake.proxies) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        f"http://{HOST}/",
        f"https://{HOST}:8443/",
        f"https://user:pw@{HOST}/",
    ],
)
async def test_only_allowlisted_https_hosts(url):
    fake = FakeUpstream(lambda req: httpx.Response(200))
    with pytest.raises(EgressError) as exc:
        await _egress(fake).send("GET", url)
    assert exc.value.code == "host_not_allowed"
    assert fake.proxies == []


async def test_fails_closed_without_zone():
    fake = FakeUpstream(lambda req: httpx.Response(200))
    with pytest.raises(EgressError) as exc:
        await _egress(fake, brightdata_zone="").send("GET", URL)
    assert exc.value.code == "proxy_not_configured"
    assert fake.proxies == []


async def test_direct_mode_when_proxy_not_required():
    fake = FakeUpstream(lambda req: httpx.Response(200, json={}))
    resp = await _egress(fake, brightdata_zone="", require_proxy=False).send("GET", URL)
    assert resp.egress.proxy == "direct"
    assert fake.proxies == [None]


async def test_response_over_cap_is_refused():
    fake = FakeUpstream(lambda req: httpx.Response(200, content=b"x" * 2048))
    with pytest.raises(EgressError) as exc:
        await _egress(fake, max_response_bytes=1024).send("GET", URL)
    assert exc.value.code == "response_too_large"


def test_proxy_url_format_and_quoting():
    url = proxy_url(_settings(brightdata_country="in"), session_id="abc123")
    parts = urlsplit(url)
    assert unquote(parts.username) == "brd-customer-hl_test-zone-resi-country-in-session-abc123"
    assert unquote(parts.password) == "p@ss/word"
    assert (parts.hostname, parts.port) == ("brd.superproxy.io", 33335)
