"""Exa, Brave and Parallel: build one HTTP request, parse the answer.

Pure functions, no I/O, so each provider's wire format is tested on its own.
Every parser returns the same result shape:

    {"title": str, "url": str, "snippets": [str, ...], "published_date": str?}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SNIPPET_MAX_CHARS = 2000  # per-result highlight budget asked of Exa
BRAVE_MAX_COUNT = 20  # Brave rejects count > 20
ERROR_DETAIL_CHARS = 300


@dataclass(frozen=True)
class ProviderRequest:
    method: Literal["GET", "POST"]
    url: str
    headers: dict[str, str]
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderFailure:
    """A provider answered with an error status."""

    code: str
    retryable: bool
    status: int
    detail: str = field(default="")


def classify_status(status: int, detail: str) -> ProviderFailure:
    if status in (401, 403):
        return ProviderFailure("auth_failed", False, status, detail)
    if status == 402:
        return ProviderFailure("quota_exhausted", False, status, detail)
    if status == 429:
        return ProviderFailure("rate_limited", True, status, detail)
    if status in (400, 404, 413, 422):
        return ProviderFailure("bad_request", False, status, detail)
    return ProviderFailure("provider_error", status >= 500, status, detail)


def _result(title: Any, url: Any, snippets: list[Any], published: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": str(title or ""),
        "url": str(url or ""),
        "snippets": [str(s) for s in snippets if s],
    }
    if published:
        item["published_date"] = str(published)
    return item


# --- Exa ---------------------------------------------------------------------


def exa_request(
    url: str,
    api_key: str,
    *,
    query: str,
    num_results: int,
    include_domains: list[str],
    exclude_domains: list[str],
    start_published_date: str,
    end_published_date: str,
) -> ProviderRequest:
    body: dict[str, Any] = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "contents": {"highlights": {"query": query, "maxCharacters": SNIPPET_MAX_CHARS}},
    }
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    if start_published_date:
        body["startPublishedDate"] = start_published_date
    if end_published_date:
        body["endPublishedDate"] = end_published_date
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    return ProviderRequest("POST", url, headers, json_body=body)


def parse_exa(data: Any) -> list[dict[str, Any]]:
    rows = data.get("results") if isinstance(data, dict) else None
    return [
        _result(r.get("title"), r.get("url"), list(r.get("highlights") or []), r.get("publishedDate"))
        for r in rows or []
        if isinstance(r, dict)
    ]


# --- Brave -------------------------------------------------------------------


def brave_request(
    url: str,
    api_key: str,
    *,
    query: str,
    num_results: int,
    country: str,
    freshness: str,
) -> ProviderRequest:
    params: dict[str, Any] = {"q": query, "count": min(num_results, BRAVE_MAX_COUNT), "extra_snippets": "true"}
    if country:
        params["country"] = country
    if freshness:
        params["freshness"] = freshness
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    return ProviderRequest("GET", url, headers, params=params)


def parse_brave(data: Any) -> list[dict[str, Any]]:
    web = data.get("web") if isinstance(data, dict) else None
    rows = web.get("results") if isinstance(web, dict) else None
    return [
        _result(
            r.get("title"),
            r.get("url"),
            [r.get("description"), *(r.get("extra_snippets") or [])],
            r.get("page_age"),
        )
        for r in rows or []
        if isinstance(r, dict)
    ]


# --- Parallel ----------------------------------------------------------------


def parallel_request(
    url: str,
    api_key: str,
    *,
    objective: str,
    search_queries: list[str],
    num_results: int,
) -> ProviderRequest:
    body: dict[str, Any] = {"objective": objective, "max_results": num_results}
    if search_queries:
        body["search_queries"] = search_queries
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    return ProviderRequest("POST", url, headers, json_body=body)


def parse_parallel(data: Any) -> list[dict[str, Any]]:
    rows = data.get("results") if isinstance(data, dict) else None
    return [
        _result(r.get("title"), r.get("url"), list(r.get("excerpts") or []), r.get("publish_date"))
        for r in rows or []
        if isinstance(r, dict)
    ]
