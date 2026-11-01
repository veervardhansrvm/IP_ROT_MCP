"""Bright Data super-proxy URLs.

The exit IP is chosen by the session id inside the proxy username: a new id
gets a new IP, so every call builds its own URL.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

from bd_search_mcp.config import Settings

SESSION_ID_BYTES = 8


def new_session_id() -> str:
    return secrets.token_hex(SESSION_ID_BYTES)


def proxy_url(settings: Settings, *, session_id: str) -> str:
    """brd-customer-<id>-zone-<zone>[-country-<cc>]-session-<sid>, credentials URL-quoted."""
    user = f"brd-customer-{settings.brightdata_customer_id}-zone-{settings.brightdata_zone}"
    if settings.brightdata_country:
        user += f"-country-{settings.brightdata_country}"
    user += f"-session-{session_id}"
    password = settings.brightdata_password.get_secret_value()
    return (
        f"http://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{settings.brightdata_host}:{settings.brightdata_port}"
    )
