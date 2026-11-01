from __future__ import annotations

import pytest

from bd_search_mcp import providers


def test_exa_request_maps_filters():
    req = providers.exa_request(
        "https://api.exa.ai/search",
        "k",
        query="rbi repo rate",
        num_results=5,
        include_domains=["rbi.org.in"],
        exclude_domains=[],
        start_published_date="2026-01-01",
        end_published_date="",
    )
    assert req.method == "POST"
    assert req.headers["x-api-key"] == "k"
    assert req.json_body["numResults"] == 5
    assert req.json_body["includeDomains"] == ["rbi.org.in"]
    assert req.json_body["startPublishedDate"] == "2026-01-01"
    assert "excludeDomains" not in req.json_body
    assert "endPublishedDate" not in req.json_body


def test_parse_exa():
    data = {"results": [{"title": "T", "url": "https://a", "highlights": ["h1"], "publishedDate": "2026-01-02"}]}
    assert providers.parse_exa(data) == [
        {"title": "T", "url": "https://a", "snippets": ["h1"], "published_date": "2026-01-02"}
    ]


def test_brave_request_caps_count_and_sets_token():
    req = providers.brave_request(
        "https://api.search.brave.com/res/v1/web/search", "k", query="q", num_results=50, country="IN", freshness="pw"
    )
    assert req.method == "GET"
    assert req.headers["X-Subscription-Token"] == "k"
    assert req.params == {"q": "q", "count": 20, "extra_snippets": "true", "country": "IN", "freshness": "pw"}


def test_parse_brave_joins_description_and_extra_snippets():
    data = {"web": {"results": [{"title": "T", "url": "https://b", "description": "d", "extra_snippets": ["e"]}]}}
    assert providers.parse_brave(data) == [{"title": "T", "url": "https://b", "snippets": ["d", "e"]}]


def test_parse_brave_without_web_block():
    assert providers.parse_brave({"type": "search"}) == []


def test_parallel_request_and_parse():
    req = providers.parallel_request(
        "https://api.parallel.ai/v1/search", "k", objective="o", search_queries=["a"], num_results=3
    )
    assert req.json_body == {"objective": "o", "max_results": 3, "search_queries": ["a"]}
    data = {"results": [{"title": None, "url": "https://c", "excerpts": ["x"], "publish_date": None}]}
    assert providers.parse_parallel(data) == [{"title": "", "url": "https://c", "snippets": ["x"]}]


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "auth_failed", False),
        (402, "quota_exhausted", False),
        (429, "rate_limited", True),
        (422, "bad_request", False),
        (503, "provider_error", True),
        (418, "provider_error", False),
    ],
)
def test_classify_status(status, code, retryable):
    f = providers.classify_status(status, "detail")
    assert (f.code, f.retryable) == (code, retryable)
