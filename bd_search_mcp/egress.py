"""Send one provider HTTP call out through a fresh Bright Data exit IP.

TLS is end to end: the proxy only sees a CONNECT tunnel, so provider keys and
results are never readable by Bright Data.

Retry rule: only failures before the request reached the provider (proxy
refused, tunnel or connect failed) are retried, each on a new session = new
IP. Anything after that (a read timeout, a provider 4xx/5xx) is returned
once, because a POST may already have been billed by the provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from bd_search_mcp.brightdata import new_session_id, proxy_url
from bd_search_mcp.config import Settings

logger = logging.getLogger(__name__)

HTTPS_PORT = 443

ClientFactory = Callable[[str | None, httpx.Timeout], httpx.AsyncClient]


class EgressError(Exception):
    """The call never produced a provider response.

    ``code`` is one of: host_not_allowed, proxy_not_configured, busy,
    proxy_error, timeout, upstream_error, response_too_large.
    """

    def __init__(self, code: str, message: str, attempts: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.attempts = code, message, attempts


@dataclass(frozen=True)
class EgressInfo:
    proxy: str  # "brightdata" | "direct"
    exit_session: str | None
    attempts: int
    latency_ms: int


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes
    egress: EgressInfo

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def text(self, limit: int) -> str:
        return self.body[:limit].decode("utf-8", errors="replace")


class _ResponseTooLarge(Exception):
    pass


def default_client_factory() -> ClientFactory:
    # One SSL context for the process: building it per call costs a CA-bundle parse each time.
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    def make(proxy: str | None, timeout: httpx.Timeout) -> httpx.AsyncClient:
        # One client per attempt because httpx binds the proxy (= the exit IP) to the client.
        # No keep-alive: a reused tunnel would keep the old exit IP.
        return httpx.AsyncClient(
            proxy=proxy,
            timeout=timeout,
            verify=ssl_context,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"Connection": "close"},
        )

    return make


class RotatingEgress:
    def __init__(
        self,
        settings: Settings,
        allowed_hosts: frozenset[str],
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._allowed_hosts = allowed_hosts
        self._client_factory = client_factory or default_client_factory()
        self._clock = clock
        self._slots = asyncio.Semaphore(settings.max_concurrent_requests)

    def check_target(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or (parts.port not in (None, HTTPS_PORT))
            or host not in self._allowed_hosts
        ):
            raise EgressError("host_not_allowed", f"{host or url!r} is not an allowed provider host")

    async def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> UpstreamResponse:
        self.check_target(url)
        if self._s.require_proxy and not self._s.proxy_configured:
            raise EgressError("proxy_not_configured", "Bright Data zone is not configured and require_proxy is on")
        try:
            await asyncio.wait_for(self._slots.acquire(), self._s.concurrency_acquire_timeout_seconds)
        except TimeoutError as exc:
            raise EgressError("busy", "too many calls in flight, retry shortly") from exc
        try:
            return await self._send_with_retries(method, url, headers, params, json_body)
        finally:
            self._slots.release()

    async def _send_with_retries(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
        json_body: Any,
    ) -> UpstreamResponse:
        s = self._s
        use_proxy = s.proxy_configured
        started = self._clock()
        deadline = started + s.timeout_seconds
        last_failure = ""
        for attempt in range(1, s.max_attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise EgressError("timeout", f"time budget spent after {attempt - 1} attempt(s)", attempt - 1)
            session = new_session_id() if use_proxy else None
            proxy = proxy_url(s, session_id=session) if session else None
            timeout = httpx.Timeout(remaining, connect=min(remaining, s.connect_timeout_seconds))
            try:
                async with (
                    self._client_factory(proxy, timeout) as client,
                    client.stream(method, url, headers=headers, params=params, json=json_body) as response,
                ):
                    body = await _read_capped(response, s.max_response_bytes)
                    info = EgressInfo(
                        proxy="brightdata" if use_proxy else "direct",
                        exit_session=session,
                        attempts=attempt,
                        latency_ms=int((self._clock() - started) * 1000),
                    )
                    return UpstreamResponse(status=response.status_code, body=body, egress=info)
            except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Never reached the provider: safe to retry on a new exit IP.
                last_failure = type(exc).__name__
                logger.warning("egress.attempt_failed attempt=%d error=%s", attempt, last_failure)
                continue
            except _ResponseTooLarge as exc:
                raise EgressError(
                    "response_too_large", f"provider response exceeded {s.max_response_bytes} bytes", attempt
                ) from exc
            except httpx.TimeoutException as exc:
                message = f"provider did not answer in time ({type(exc).__name__})"
                raise EgressError("timeout", message, attempt) from exc
            except httpx.HTTPError as exc:
                raise EgressError("upstream_error", type(exc).__name__, attempt) from exc
        raise EgressError("proxy_error", f"all {s.max_attempts} attempts failed ({last_failure})", s.max_attempts)


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _ResponseTooLarge
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise _ResponseTooLarge
    return bytes(buf)
