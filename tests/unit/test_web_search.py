"""Unit tests for WebSearchClient (Tavily transport mocked)."""
from __future__ import annotations

import httpx
import pytest

from exceptions import MCPAuthError, MCPError, MCPRateLimitError
from mcp_tools.web_search import SearchResults, WebSearchClient

TAVILY_OK = {
    "answer": "The cheapest tickets are category 4.",
    "results": [
        {"title": "WC Tickets", "url": "https://x/tickets", "content": "Cat-4 from $X", "score": 0.9},
        {"title": "Hotel", "url": "https://x/hotel", "content": "8.9 rating", "score": 0.7},
    ],
}


def _client(handler) -> WebSearchClient:
    client = WebSearchClient(api_key="k", api_url="https://api.tavily.com/search")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._session = client._http
    return client


async def test_search_parses_results():
    client = _client(lambda req: httpx.Response(200, json=TAVILY_OK))
    out = await client.search("cheapest world cup ticket", max_results=5)
    assert isinstance(out, SearchResults)
    assert out.answer == "The cheapest tickets are category 4."
    assert len(out.results) == 2
    assert out.results[0].url == "https://x/tickets"


async def test_search_sends_api_key_and_query():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=TAVILY_OK)

    client = _client(handler)
    await client.search("hotels near stadium", max_results=3)
    assert seen["api_key"] == "k"
    assert seen["query"] == "hotels near stadium"
    assert seen["max_results"] == 3


async def test_search_auth_error_maps_to_typed():
    client = _client(lambda req: httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(MCPAuthError):
        await client.search("anything")


async def test_search_empty_results_ok():
    client = _client(lambda req: httpx.Response(200, json={"results": []}))
    out = await client.search("nothing")
    assert out.results == []
    assert out.answer is None


async def test_search_rate_limit_maps_to_typed():
    client = _client(lambda req: httpx.Response(429, json={"detail": "slow down"}))
    with pytest.raises(MCPRateLimitError):
        await client.search("anything")
