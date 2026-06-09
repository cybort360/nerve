"""WebSearchClient: a general web-research hand backed by the Tavily API.

Like :class:`~mcp_tools.gitlab.GitLabClient`, this is a ``BaseMCPClient`` subclass
that talks REST (the Tavily Search API) rather than a real MCP server, so every
call still flows through the wrapped path: audit events, retry, failure
injection, and typed :class:`~exceptions.MCPError` translation. The single tool
``search`` is mapped to one HTTPS POST by the overridden :meth:`_raw_call`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from config import settings
from exceptions import (
    MCPAuthError,
    MCPConnectionError,
    MCPError,
    MCPRateLimitError,
    MCPToolCallError,
)
from mcp_tools.base_client import BaseMCPClient

SERVER_WEB_SEARCH = "web_search"
TOOL_SEARCH = "search"
HTTP_TIMEOUT_SECONDS = 25.0


class SearchResult(BaseModel):
    """One web search hit."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    url: str = ""
    content: str = ""
    score: float | None = None


class SearchResults(BaseModel):
    """A parsed Tavily response: an optional synthesized answer plus hits."""

    model_config = ConfigDict(extra="ignore")

    answer: str | None = None
    results: list[SearchResult] = Field(default_factory=list)


class WebSearchClient(BaseMCPClient):
    """Searches the web via Tavily; one ``search`` tool, REST under the hood."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        *,
        mission_id: str | None = None,
        failure_engine: Any | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            api_key: Tavily API key (defaults to ``settings.tavily_api_key``).
            api_url: Tavily search endpoint (defaults to ``settings.tavily_api_url``).
            mission_id: Mission to attribute audit events to (optional).
            failure_engine: FailureEngine whose modifications to apply (optional).
        """
        self._api_key = api_key if api_key is not None else settings.tavily_api_key
        super().__init__(
            SERVER_WEB_SEARCH,
            api_url or settings.tavily_api_url,
            {},
            mission_id=mission_id,
            failure_engine=failure_engine,
        )
        self._http: Any | None = None

    async def connect(self) -> None:
        """Open the httpx client against the Tavily endpoint."""
        import httpx  # lazy: only needed for live calls

        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        self._session = self._http
        self._log.info("web_search_connected", url=self._server_url)

    async def disconnect(self) -> None:
        """Close the httpx client."""
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._session = None
        self._log.info("web_search_disconnected")

    async def _raw_call(self, tool_name: str, arguments: dict) -> dict:
        """Issue one Tavily request for the ``search`` tool, translating errors.

        Args:
            tool_name: Must be ``"search"``; any other name raises MCPToolCallError.
            arguments: Must include ``query``; optionally ``max_results``.

        Returns:
            The parsed Tavily JSON response as a dict.

        Raises:
            MCPError: Typed error for auth/rate-limit/transport failures.
        """
        if tool_name != TOOL_SEARCH:
            raise MCPToolCallError(
                f"unknown web-search tool: {tool_name}",
                context={"tool": tool_name},
                recoverable=False,
            )
        if not self._api_key:
            raise MCPAuthError(
                "TAVILY_API_KEY is not set", context={"server": self.server_name}
            )
        if self._http is None:
            await self.connect()
        body = {
            "api_key": self._api_key,
            "query": arguments["query"],
            "max_results": arguments.get("max_results", settings.web_search_max_results),
            "include_answer": True,
        }
        try:
            response = await self._http.post(self._server_url, json=body)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 — translate any transport error
            raise MCPConnectionError(
                "tavily request failed",
                context={"server": self.server_name, "error": str(exc)},
                recoverable=True,
            ) from exc
        if response.status_code >= 400:
            raise self._http_error(response.status_code)
        return response.json() if response.content else {}

    def _http_error(self, status: int) -> MCPError:
        """Map a Tavily HTTP status code to a typed MCPError.

        Args:
            status: HTTP status code returned by Tavily.

        Returns:
            A typed :class:`~exceptions.MCPError` subclass.
        """
        ctx = {"server": self.server_name, "status": status}
        if status in (401, 403):
            return MCPAuthError("Tavily authentication failed", context=ctx)
        if status == 429:
            return MCPRateLimitError("Tavily rate limited", context=ctx)
        if status >= 500:
            return MCPConnectionError(f"Tavily server error ({status})", context=ctx)
        return MCPToolCallError(f"Tavily returned {status}", context=ctx, recoverable=False)

    async def search(self, query: str, max_results: int | None = None) -> SearchResults:
        """Search the web for a query and return parsed results.

        Args:
            query: The search query.
            max_results: Optional cap on hits (defaults to the configured value).

        Returns:
            A :class:`SearchResults` with an optional answer and a list of hits.

        Raises:
            MCPError: A typed error on transport/auth/rate-limit failure.
        """
        resolved = max_results if max_results is not None else settings.web_search_max_results
        args = {"query": query, "max_results": resolved}
        raw = await self.call_tool(TOOL_SEARCH, args)
        try:
            return SearchResults.model_validate(raw)
        except PydanticValidationError as exc:
            raise MCPToolCallError(
                "could not parse Tavily response",
                context={"server": self.server_name, "error": str(exc)},
                recoverable=False,
            ) from exc
