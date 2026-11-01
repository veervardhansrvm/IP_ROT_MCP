"""Settings, read once from the environment (or a local .env file).

Every tunable and secret lives here; nothing else reads os.environ.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- server
    bind_host: str = Field("0.0.0.0", description="Interface the HTTP server binds to.")  # noqa: S104  # nosec B104 - container listens on all interfaces
    port: int = Field(8080, description="HTTP port. MCP endpoint is /mcp, health is /healthz.")
    log_level: str = Field("INFO", description="Python logging level.")
    auth_token: SecretStr = Field(
        SecretStr(""),
        description="Bearer token callers must send on /mcp. Empty = no auth (private network only).",
    )

    # --- Bright Data super-proxy (use a residential or datacenter zone, not Web Unlocker / SERP)
    brightdata_customer_id: str = Field("", description="Account id, e.g. hl_1a2b3c4d.")
    brightdata_zone: str = Field("", description="Zone name from the Bright Data dashboard.")
    brightdata_password: SecretStr = Field(SecretStr(""), description="Zone password.")
    brightdata_host: str = Field("brd.superproxy.io", description="Super-proxy host.")
    brightdata_port: int = Field(33335, description="Super-proxy port (older accounts: 22225).")
    brightdata_country: str = Field(
        "", pattern=r"^([a-z]{2})?$", description="Exit country, ISO alpha-2 lower case. Empty = any."
    )
    require_proxy: bool = Field(
        True, description="Fail closed when the zone is not configured. False = go out directly (dev only)."
    )

    # --- provider keys (a tool whose key is empty answers not_configured)
    exa_api_key: SecretStr = Field(SecretStr(""), description="Exa API key.")
    brave_api_key: SecretStr = Field(SecretStr(""), description="Brave Search API subscription token.")
    parallel_api_key: SecretStr = Field(SecretStr(""), description="Parallel API key.")

    # --- provider endpoints (also the egress allowlist: only these hosts are reachable)
    exa_search_url: str = Field("https://api.exa.ai/search", description="Exa search endpoint.")
    brave_search_url: str = Field(
        "https://api.search.brave.com/res/v1/web/search", description="Brave web search endpoint."
    )
    parallel_search_url: str = Field("https://api.parallel.ai/v1/search", description="Parallel search endpoint.")

    # --- limits
    max_attempts: int = Field(
        3, ge=1, le=10, description="Tries per call; only connection/proxy failures retry, each on a new exit IP."
    )
    timeout_seconds: float = Field(30, gt=0, description="Total budget for one tool call, all attempts included.")
    connect_timeout_seconds: float = Field(10, gt=0, description="Per-attempt cap on proxy connect + tunnel setup.")
    max_response_bytes: int = Field(5 * 1024 * 1024, gt=0, description="Largest provider response read.")
    max_concurrent_requests: int = Field(64, ge=1, description="Outbound calls in flight across all tools.")
    concurrency_acquire_timeout_seconds: float = Field(
        5, gt=0, description="Wait for a free slot before answering busy."
    )

    @property
    def proxy_configured(self) -> bool:
        return bool(
            self.brightdata_customer_id and self.brightdata_zone and self.brightdata_password.get_secret_value()
        )

    @property
    def provider_hosts(self) -> frozenset[str]:
        urls = (self.exa_search_url, self.brave_search_url, self.parallel_search_url)
        return frozenset((urlsplit(u).hostname or "").lower() for u in urls)


@lru_cache
def get_settings() -> Settings:
    return Settings()
