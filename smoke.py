"""Live check against your real Bright Data zone and provider keys (reads .env).

  1. 10 calls to an IP-echo endpoint      -> expect ~10 different exit IPs
  2. each search tool whose key is set    -> expect results, proxy=brightdata

Run:  .\\run.ps1 smoke
"""

from __future__ import annotations

import asyncio
import sys

from bd_search_mcp import server
from bd_search_mcp.config import get_settings
from bd_search_mcp.egress import EgressError, RotatingEgress

ECHO_HOST = "api.ipify.org"
ECHO_CALLS = 10
PASS_RATIO = 0.8


async def rotation_check() -> bool:
    s = get_settings()
    egress = RotatingEgress(s, frozenset({ECHO_HOST}))  # test-only allowlist
    ips = []
    print(f"== 1. {ECHO_CALLS} calls to {ECHO_HOST}: expect different IPs ==")
    for i in range(ECHO_CALLS):
        try:
            r = await egress.send("GET", f"https://{ECHO_HOST}", params={"format": "json"})
            ip = r.json()["ip"]
            ips.append(ip)
            print(f"  #{i:<2} {ip:<40} attempts={r.egress.attempts} {r.egress.latency_ms} ms")
        except EgressError as e:
            print(f"  #{i:<2} ERROR {e.code}: {e.message}")
    unique = len(set(ips))
    print(f"  -> {unique} unique IPs out of {ECHO_CALLS}\n")
    return unique >= PASS_RATIO * ECHO_CALLS


async def tool_check() -> None:
    print("== 2. search tools ==")
    calls = [
        ("brave", server.brave_search(query="RBI repo rate", num_results=3)),
        ("exa", server.exa_search(query="RBI repo rate", num_results=3)),
        ("parallel", server.parallel_search(objective="Current RBI repo rate", num_results=3)),
    ]
    for name, coro in calls:
        out = await coro
        if "error" in out:
            print(f"  {name:<9} ERROR {out['error']}: {out['message'][:150]}")
        else:
            first = out["results"][0]["url"] if out["results"] else "(no results)"
            e = out["egress"]
            print(
                f"  {name:<9} {len(out['results'])} results via {e['proxy']} "
                f"attempts={e['attempts']} {e['latency_ms']} ms  first={first}"
            )


async def main() -> int:
    s = get_settings()
    print(
        f"proxy: {'brightdata' if s.proxy_configured else 'NOT CONFIGURED'} "
        f"{s.brightdata_host}:{s.brightdata_port} country={s.brightdata_country or 'any'}\n"
    )
    ok = await rotation_check()
    await tool_check()
    print(f"\nVERDICT: {'PASS - each call left from a new IP' if ok else 'FAIL - see rotation results'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
