# General Web-Research Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `GENERAL` NERVE mission do real web research via Tavily and hand off a recommendation through the existing human-approval gate.

**Architecture:** A new `WebSearchClient` (subclass of `BaseMCPClient`, REST-over-httpx like `GitLabClient`) gives the `ExecutionAgent` a general "search the web" hand. The `MissionPlanner` emits structured tool intents (`tool` + `tool_args`) on tasks so search queries are explicit. After a `GENERAL` mission's tasks complete, a small `research_concierge` synthesis step turns the gathered results into a recommendation and creates a `human_approval_request` action — which the dashboard and Telegram already render. No headless browser, no auto-checkout.

**Tech Stack:** Python 3.11+, async/await, httpx, Pydantic v2, structlog, tenacity, pytest (asyncio_mode=auto), Tavily Search API, Gemini (via existing planner seam).

---

## File Structure

| File | Responsibility | New/Modified |
|---|---|---|
| `config.py` | Add `tavily_api_key`, `web_search_max_results`, `tavily_api_url` | Modified |
| `.env.example` | Document the Tavily key | Modified |
| `mcp_tools/web_search.py` | `WebSearchClient` + typed result models | **New** |
| `state/models.py` | Add optional `tool`/`tool_args` to `Task` | Modified |
| `orchestrator/planner.py` | `TaskDefinition` gains `tool`/`tool_args`; prompt teaches the planner; `_to_tasks` threads them | Modified |
| `agents/execution_agent.py` | Build with a `web_search` client; route web-search tasks; prefer `task.tool` | Modified |
| `orchestrator/orchestrator.py` | Pass `task.tool_args` into the agent; wire `web_search` client; call synthesis for GENERAL | Modified |
| `modules/research_concierge/__init__.py` | Package marker | **New** |
| `modules/research_concierge/synthesis.py` | `synthesize_and_handoff()` → recommendation + approval action | **New** |
| `tests/unit/test_web_search.py` | WebSearchClient unit tests | **New** |
| `tests/unit/test_planner_tool_intents.py` | Planner emits tool intents | **New** |
| `tests/unit/test_execution_web_search.py` | ExecutionAgent dispatches web_search | **New** |
| `tests/unit/test_research_synthesis.py` | Synthesis creates the hand-off action | **New** |

> **Commit note:** This repo is not currently a git repository. If `git` commands fail, run `git init` first (the user has approved committing this work), or skip the commit steps and batch-commit at the end. Each task below still ends with a logical commit boundary.

---

### Task 1: Tavily configuration

**Files:**
- Modify: `config.py` (add settings near the other provider blocks, before Feature Flags)
- Modify: `.env.example` (add a Web Search block)

- [ ] **Step 1: Add settings fields**

In `config.py`, find the `Settings` class and add these fields alongside the existing GitLab/Dynatrace settings (match the surrounding style — they are `pydantic-settings` fields):

```python
    # Web Search (Tavily) — powers GENERAL-mission research.
    tavily_api_key: str = ""
    tavily_api_url: str = "https://api.tavily.com/search"
    web_search_max_results: int = 5
```

- [ ] **Step 2: Document in .env.example**

In `.env.example`, add after the GitLab block:

```
# Web Search (Tavily) — required for GENERAL research missions.
# Get a free key at https://app.tavily.com (free tier ~1,000 searches/month).
TAVILY_API_KEY=
```

- [ ] **Step 3: Verify config imports cleanly**

Run: `venv/bin/python -c "from config import settings; print(settings.tavily_api_url, settings.web_search_max_results)"`
Expected: prints `https://api.tavily.com/search 5`

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "feat: add Tavily web-search configuration"
```

---

### Task 2: WebSearchClient

**Files:**
- Create: `mcp_tools/web_search.py`
- Test: `tests/unit/test_web_search.py`

This mirrors `mcp_tools/gitlab.py`: subclass `BaseMCPClient`, override `connect`/`disconnect` to manage an `httpx.AsyncClient`, override `_raw_call` to POST to Tavily, and expose one typed `search()` method that goes through `call_tool` (so audit/retry/failure-injection still apply).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_web_search.py`:

```python
"""Unit tests for WebSearchClient (Tavily transport mocked)."""
from __future__ import annotations

import httpx
import pytest

from exceptions import MCPAuthError, MCPError
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_web_search.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_tools.web_search'`

- [ ] **Step 3: Write the implementation**

Create `mcp_tools/web_search.py`:

```python
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
            server_name=SERVER_WEB_SEARCH,
            server_url=api_url or settings.tavily_api_url,
            auth_headers={},
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
        """Issue one Tavily request for the ``search`` tool, translating errors."""
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
            raise MCPConnectionError(
                "web-search client not connected",
                context={"server": self.server_name, "tool": tool_name},
            )
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
        """Map a Tavily HTTP status code to a typed MCPError."""
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
        args = {"query": query, "max_results": max_results or settings.web_search_max_results}
        raw = await self.call_tool(TOOL_SEARCH, args)
        try:
            return SearchResults.model_validate(raw)
        except PydanticValidationError as exc:
            raise MCPToolCallError(
                "could not parse Tavily response",
                context={"server": self.server_name, "error": str(exc)},
                recoverable=False,
            ) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_web_search.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mcp_tools/web_search.py tests/unit/test_web_search.py
git commit -m "feat: add WebSearchClient backed by Tavily"
```

---

### Task 3: Thread tool intents through the Task model

**Files:**
- Modify: `state/models.py` (the `Task` model)
- Test: `tests/unit/test_task_tool_fields.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_task_tool_fields.py`:

```python
from state.models import Task


def test_task_defaults_have_no_tool():
    t = Task(mission_id="m", agent_role="execution", description="do thing")
    assert t.tool is None
    assert t.tool_args == {}


def test_task_accepts_tool_intent():
    t = Task(
        mission_id="m",
        agent_role="execution",
        description="search the web",
        tool="web_search",
        tool_args={"query": "cheapest tickets"},
    )
    assert t.tool == "web_search"
    assert t.tool_args["query"] == "cheapest tickets"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_task_tool_fields.py -q`
Expected: FAIL — `Task` has no field `tool` (or a validation error).

- [ ] **Step 3: Add the fields**

In `state/models.py`, find the `Task` model and add these two optional fields (place them after `description`, before `status` to match field ordering):

```python
    tool: str | None = None
    tool_args: dict = Field(default_factory=dict)
```

(`Field` is already imported in this module; if not, add `from pydantic import Field`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_task_tool_fields.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add state/models.py tests/unit/test_task_tool_fields.py
git commit -m "feat: add optional tool/tool_args intent to Task model"
```

---

### Task 4: Planner emits tool intents

**Files:**
- Modify: `orchestrator/planner.py` (`TaskDefinition`, `_PROMPT_TEMPLATE`, `_to_tasks`)
- Test: `tests/unit/test_planner_tool_intents.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_planner_tool_intents.py`:

```python
import json

from orchestrator.planner import MissionPlanner

PLAN_JSON = json.dumps([
    {"description": "Search for cheapest 2026 World Cup tickets",
     "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "cheapest 2026 World Cup tickets"}},
    {"description": "Search hotels near the stadium with good reviews",
     "agent_role": "execution", "depends_on": [],
     "tool": "web_search", "tool_args": {"query": "cheap well-reviewed hotel near World Cup stadium"}},
])


async def test_planner_carries_tool_intents_onto_tasks():
    async def fake_generate(prompt: str) -> str:
        return PLAN_JSON

    planner = MissionPlanner(generate=fake_generate)
    tasks = await planner.plan("m1", "find cheap ticket + hotel", {})
    assert all(t.tool == "web_search" for t in tasks)
    assert tasks[0].tool_args["query"].startswith("cheapest")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_planner_tool_intents.py -q`
Expected: FAIL — tasks have `tool is None` (definitions don't carry the fields yet).

- [ ] **Step 3: Extend TaskDefinition and _to_tasks**

In `orchestrator/planner.py`, add the two fields to `TaskDefinition`:

```python
class TaskDefinition(BaseModel):
    """A single decomposed task definition (the /internal/decompose contract)."""

    description: str
    agent_role: AgentRole
    depends_on: list[int] = Field(default_factory=list)
    tool: str | None = None
    tool_args: dict = Field(default_factory=dict)
```

Then update `_to_tasks` to copy them onto the `Task`:

```python
    @staticmethod
    def _to_tasks(mission_id: str, definitions: list[TaskDefinition]) -> list[Task]:
        """Convert definitions into Task models, resolving index dependencies."""
        tasks = [
            Task(
                mission_id=mission_id,
                agent_role=d.agent_role,
                description=d.description,
                tool=d.tool,
                tool_args=d.tool_args,
            )
            for d in definitions
        ]
        for task, definition in zip(tasks, definitions):
            for idx in definition.depends_on:
                if 0 <= idx < len(tasks) and tasks[idx] is not task:
                    task.depends_on.append(tasks[idx].task_id)
        return tasks
```

- [ ] **Step 4: Teach the planner about the web_search tool**

In `orchestrator/planner.py`, update `_PROMPT_TEMPLATE` so the model emits tool intents. Replace the existing template with:

```python
_PROMPT_TEMPLATE = (
    "You are nerve-planner, decomposing an operational goal into tasks.\n"
    "Respond with ONLY a JSON array. Each element is an object with keys: "
    "'description' (string), 'agent_role' (one of {roles}), 'depends_on' "
    "(array of zero-based indices of earlier tasks), and — for research tasks — "
    "'tool' and 'tool_args'.\n"
    "The available tool is 'web_search' with tool_args {{\"query\": <string>}}. "
    "For any task that requires finding information on the internet, set "
    "agent_role to 'execution', tool to 'web_search', and put a focused search "
    "query in tool_args.query. For non-tool tasks omit 'tool'.\n"
    "Produce at least {min_tasks} tasks.\n\nGOAL: {goal}\nCONTEXT: {context}\n"
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_planner_tool_intents.py tests/unit -q -k "planner"`
Expected: PASS — including the existing planner tests (no regressions).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/planner.py tests/unit/test_planner_tool_intents.py
git commit -m "feat: planner emits web_search tool intents"
```

---

### Task 5: ExecutionAgent dispatches web_search

**Files:**
- Modify: `agents/execution_agent.py` (`__init__`, `execute`, `_resolve`, `_routes`)
- Modify: `orchestrator/orchestrator.py` (`_run_one`, `_default_execution_agent`)
- Test: `tests/unit/test_execution_web_search.py` (new)

The agent currently routes by description keywords. We add a `web_search` client and prefer an explicit `task.tool` when present, falling back to keyword routing for incident tasks. The orchestrator passes `task.tool_args` through.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_execution_web_search.py`:

```python
"""ExecutionAgent routes a web_search task to the web-search client."""
from __future__ import annotations

import pytest

from agents.execution_agent import ExecutionAgent
from mcp_tools.web_search import SearchResult, SearchResults
from state.models import Task


class _FakeWebSearch:
    def __init__(self):
        self.calls = []

    async def search(self, query: str, max_results: int | None = None) -> SearchResults:
        self.calls.append((query, max_results))
        return SearchResults(answer="ok", results=[SearchResult(title="t", url="u", content="c")])


@pytest.fixture
async def seeded_task(mongo):  # `mongo` = existing mongomock fixture in conftest
    from state import database as db
    mission = await db.create_mission("g", "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="Search for cheapest tickets", tool="web_search",
        tool_args={"query": "cheapest tickets"},
    )
    await db.add_tasks([task])
    return mission.mission_id, task


async def test_web_search_task_invokes_client(seeded_task):
    mission_id, task = seeded_task
    web = _FakeWebSearch()
    agent = ExecutionAgent(mission_id, dynatrace=None, gitlab=None, web_search=web)
    result = await agent.run({"task": task, "tool_args": task.tool_args})
    assert result.status == "success"
    assert web.calls == [("cheapest tickets", None)]
```

> If the conftest fixture for the in-memory DB is named differently than `mongo`, use that name. Check `tests/conftest.py` for the fixture that yields a connected mongomock database and substitute it.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_execution_web_search.py -q`
Expected: FAIL — `ExecutionAgent.__init__()` got an unexpected keyword argument `web_search`.

- [ ] **Step 3: Add the web_search client to the agent**

In `agents/execution_agent.py`, update `__init__` to accept and store a web-search client (keyword-only, defaults to `None` for back-compat):

```python
    def __init__(
        self,
        mission_id: str,
        dynatrace: Any,
        gitlab: Any,
        *,
        web_search: Any | None = None,
        max_attempts: int = 3,
        backoff_min: float = 2.0,
        backoff_max: float = 10.0,
    ) -> None:
```

Inside the body, after `self.gitlab = gitlab`, add:

```python
        self.web_search = web_search
```

(Update the docstring's Args to mention `web_search: Web-search client (async ``search`` method).`)

- [ ] **Step 4: Prefer an explicit tool, then keyword-route**

In `agents/execution_agent.py`, change `execute` to resolve from the task's explicit `tool` first. Replace the `plan = self._resolve(task.description)` line in `execute` with:

```python
        plan = self._resolve_tool(task.tool) or self._resolve(task.description)
```

Then add a new method next to `_resolve`:

```python
    def _resolve_tool(self, tool: str | None) -> ToolPlan | None:
        """Map an explicit task.tool name to a ToolPlan (None when unset/unknown)."""
        if tool == "web_search" and self.web_search is not None:
            return ToolPlan(self.web_search.search, mutating=False, name="web_search", action_type=None)
        return None
```

(No change to `_routes` — incident routing is untouched.)

- [ ] **Step 5: Pass tool_args from the task in the orchestrator**

In `orchestrator/orchestrator.py`, update `_run_one` to forward the task's tool args:

```python
    async def _run_one(self, mission_id: str, agent: ExecutionAgent, task: Task) -> None:
        """Transition one task to in-progress, emit TASK_STARTED, and execute it."""
        await db.update_task_status(task.task_id, "in_progress")
        await db.emit_event(
            mission_id, EVENT_TASK_STARTED, {"task_id": task.task_id}, SOURCE_ORCH, task_id=task.task_id
        )
        await agent.run({"task": task, "tool_args": task.tool_args})
```

And build the agent with a web-search client in `_default_execution_agent`:

```python
    def _default_execution_agent(self, mission_id: str) -> ExecutionAgent:
        """Build an ExecutionAgent with MCP clients bound to the mission."""
        dynatrace, gitlab = self._build_clients(mission_id)  # keep existing client construction
        from mcp_tools.web_search import WebSearchClient
        web_search = WebSearchClient(mission_id=mission_id, failure_engine=self._failure_engine)
        return ExecutionAgent(mission_id, dynatrace, gitlab, web_search=web_search)
```

> Look at the current body of `_default_execution_agent` (around `orchestrator/orchestrator.py:301`) and keep its existing dynatrace/gitlab construction exactly as-is; only add the `web_search` client and pass it through. The WebSearchClient must be `connect()`-ed before use if the existing clients are entered as context managers — match whatever lifecycle pattern the existing dynatrace/gitlab clients use in this method.

- [ ] **Step 6: Run test to verify it passes**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_execution_web_search.py -q`
Expected: PASS (1 passed)

- [ ] **Step 7: Run the full unit suite (no regressions)**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit -q`
Expected: PASS — all previously-passing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add agents/execution_agent.py orchestrator/orchestrator.py tests/unit/test_execution_web_search.py
git commit -m "feat: ExecutionAgent dispatches web_search tasks"
```

---

### Task 6: Synthesis + hand-off for GENERAL missions

**Files:**
- Create: `modules/research_concierge/__init__.py`
- Create: `modules/research_concierge/synthesis.py`
- Modify: `orchestrator/orchestrator.py` (`_check_resolution`)
- Test: `tests/unit/test_research_synthesis.py` (new)

After a `GENERAL` mission's tasks complete, gather their results, make one Gemini reasoning call to produce a recommendation, and create a `human_approval_request` action carrying it. This reuses the exact action-creation pattern in `modules/incident_autopilot/workflow.py` (look at `_create_rollback_action` there for the canonical `db.create_action(...)` + `EVENT_ACTION_CREATED` emit + `telegram_notifier.send_approval_request(...)` sequence, and replicate it for `action_type="human_approval_request"`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_research_synthesis.py`:

```python
"""Synthesis creates a human_approval_request hand-off action."""
from __future__ import annotations

import pytest

from modules.research_concierge.synthesis import synthesize_and_handoff
from state import database as db
from state.models import Task


async def _seed_completed_general(mission_id_goal="find cheap ticket + hotel"):
    mission = await db.create_mission(mission_id_goal, "GENERAL")
    task = Task(
        mission_id=mission.mission_id, agent_role="execution",
        description="Search tickets", tool="web_search",
        tool_args={"query": "cheapest ticket"},
        status="completed",
        result={"answer": "Cheapest is $100 at https://x/buy", "results": []},
    )
    await db.add_tasks([task])
    return mission.mission_id


async def test_synthesis_creates_approval_action():
    mission_id = await _seed_completed_general()

    async def fake_generate(prompt: str) -> str:
        return "Cheapest ticket: $100 https://x/buy. Best hotel: Y 8.9 https://x/hotel"

    await synthesize_and_handoff(mission_id, generate=fake_generate)

    actions = await db.get_actions_for_mission(mission_id, status="pending")
    assert len(actions) == 1
    assert actions[0].action_type == "human_approval_request"
    assert "Cheapest ticket" in actions[0].payload["recommendation"]


async def test_synthesis_survives_generation_failure():
    mission_id = await _seed_completed_general()

    async def boom(prompt: str) -> str:
        raise RuntimeError("gemini down")

    # Must not raise — failures are logged, mission state stays intact.
    await synthesize_and_handoff(mission_id, generate=boom)
    actions = await db.get_actions_for_mission(mission_id, status="pending")
    assert actions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_research_synthesis.py -q`
Expected: FAIL — `No module named 'modules.research_concierge'`.

- [ ] **Step 3: Create the package marker**

Create `modules/research_concierge/__init__.py` with a one-line docstring:

```python
"""Research concierge: synthesizes GENERAL-mission findings into a hand-off."""
```

- [ ] **Step 4: Implement synthesis**

Create `modules/research_concierge/synthesis.py`. Mirror the action-creation sequence from `modules/incident_autopilot/workflow.py::_create_rollback_action` (read it first to copy the exact `db.create_action` signature, the `EVENT_ACTION_CREATED` constant import, and the `telegram_notifier.send_approval_request` call). Implementation:

```python
"""Turn a completed GENERAL mission's task results into a hand-off action."""
from __future__ import annotations

from typing import Awaitable, Callable

import structlog

from state import database as db

log = structlog.get_logger().bind(component="research_concierge")

ACTION_HUMAN_APPROVAL = "human_approval_request"
EVENT_RESEARCH_SYNTHESIZED = "RESEARCH_SYNTHESIZED"
SOURCE_ORCH = "orchestrator"

_PROMPT = (
    "You are NERVE's research concierge. Given the goal and the raw findings "
    "from web searches, write a concise recommendation (max 6 lines) naming the "
    "best option(s) with prices and the exact links to proceed. Findings:\n"
    "GOAL: {goal}\nFINDINGS:\n{findings}\n"
)

GenerateFn = Callable[[str], Awaitable[str]]


async def synthesize_and_handoff(mission_id: str, *, generate: GenerateFn | None = None) -> None:
    """Synthesize completed-task findings and create a hand-off approval action.

    Never raises: any failure is logged so the mission can still resolve.

    Args:
        mission_id: The GENERAL mission whose tasks have completed.
        generate: Async text generator (defaults to the planner's Gemini caller).
    """
    try:
        await _run(mission_id, generate or _default_generate())
    except Exception as exc:  # noqa: BLE001 — synthesis must not crash the loop
        log.warning("synthesis_failed", mission_id=mission_id, error=str(exc))


async def _run(mission_id: str, generate: GenerateFn) -> None:
    """Gather findings, reason once, and create the approval action."""
    state = await db.get_mission_state(mission_id)
    if state is None:
        return
    findings = _collect_findings(state.tasks)
    if not findings:
        log.info("synthesis_no_findings", mission_id=mission_id)
        return
    prompt = _PROMPT.format(goal=state.mission.goal, findings=findings)
    recommendation = await generate(prompt)
    await _create_handoff(mission_id, recommendation)


def _collect_findings(tasks: list) -> str:
    """Concatenate completed web-search task results into a prompt-ready string."""
    lines: list[str] = []
    for task in tasks:
        if task.status != "completed" or not task.result:
            continue
        answer = task.result.get("answer") if isinstance(task.result, dict) else None
        if answer:
            lines.append(f"- {task.description}: {answer}")
    return "\n".join(lines)


async def _create_handoff(mission_id: str, recommendation: str) -> None:
    """Create the human_approval_request action and emit the audit event."""
    action = await db.create_action(
        mission_id,
        ACTION_HUMAN_APPROVAL,
        {"recommendation": recommendation},
    )
    await db.emit_event(
        mission_id,
        EVENT_RESEARCH_SYNTHESIZED,
        {"action_id": action.action_id},
        SOURCE_ORCH,
    )
    _notify_telegram(recommendation)
    log.info("synthesis_handoff_created", mission_id=mission_id, action_id=action.action_id)


def _default_generate() -> GenerateFn:
    """Return the same Gemini text generator the planner uses (single seam)."""
    from orchestrator.planner import MissionPlanner

    return MissionPlanner()._default_generate


def _notify_telegram(recommendation: str) -> None:
    """Best-effort Telegram hand-off ping; never raises."""
    try:
        from notifications.telegram_bot import telegram_notifier

        telegram_notifier.send_approval_request(recommendation)
    except Exception as exc:  # noqa: BLE001 — notifications are best-effort
        log.warning("synthesis_telegram_failed", error=str(exc))
```

> **Verify before relying on it:** open `modules/incident_autopilot/workflow.py` and `state/database.py` to confirm (a) the exact `db.create_action(...)` parameter order/return type and (b) the real `telegram_notifier.send_approval_request(...)` signature. Adjust the two calls above to match the real signatures — do not invent parameters. If `create_action` returns something other than an object with `.action_id`, adapt accordingly.

- [ ] **Step 5: Hook synthesis into GENERAL resolution**

In `orchestrator/orchestrator.py`, update `_check_resolution` to run synthesis for GENERAL missions before resolving:

```python
    async def _check_resolution(self, mission: Mission, state: MissionState) -> None:
        """Resolve or fail the mission once all tasks reach a terminal status."""
        graph = MissionGraph(state.tasks)
        if not graph.is_complete():
            return
        all_completed = all(t.status == "completed" for t in state.tasks)
        if all_completed and mission.mission_type == "GENERAL":
            from modules.research_concierge.synthesis import synthesize_and_handoff
            await synthesize_and_handoff(mission.mission_id)
        await self._set_status(mission, "resolved" if all_completed else "failed")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit/test_research_synthesis.py -q`
Expected: PASS (2 passed)

- [ ] **Step 7: Full suite**

Run: `PYTHONPATH=. venv/bin/python -m pytest tests/unit -q`
Expected: PASS — no regressions.

- [ ] **Step 8: Commit**

```bash
git add modules/research_concierge/ orchestrator/orchestrator.py tests/unit/test_research_synthesis.py
git commit -m "feat: synthesize GENERAL findings into a hand-off approval action"
```

---

### Task 7: End-to-end verification on the offline harness

**Files:**
- No production code changes; manual + optional stub.

- [ ] **Step 1: Set credentials (one-time, user actions)**

The user must run (already flagged):
```
gcloud auth application-default login
```
and paste a real `TAVILY_API_KEY=...` into `.env`.

- [ ] **Step 2: Start the offline server**

Run: `venv/bin/python run_local_demo.py --port 8080` (background)
Expected log line: `nerve_startup_complete`.

- [ ] **Step 3: Launch a GENERAL mission**

Run:
```bash
curl -s -X POST http://localhost:8080/missions -H "Content-Type: application/json" \
  -d '{"goal":"Find the cheapest 2026 World Cup ticket and a cheap, well-reviewed hotel nearby","mission_type":"GENERAL"}'
```
Expected: HTTP 201 with a `mission_id` and `status: pending`.

- [ ] **Step 4: Inspect the result after ~20-40s**

Run: `curl -s http://localhost:8080/missions/<mission_id>` and confirm:
- mission `status` is `resolved`,
- tasks tagged `tool: web_search` are `completed` with `result.answer` populated,
- one `pending_actions` entry of type `human_approval_request` whose `payload.recommendation` names options + links,
- a `RESEARCH_SYNTHESIZED` event in `recent_events`.

Also confirm the Telegram hand-off DM arrived (since `.env` has `TELEGRAM_ENABLED=true`).

- [ ] **Step 5: View in the dashboard**

Open `http://localhost:8080/?mission=<mission_id>` and confirm the recommendation shows as a pending approval card.

- [ ] **Step 6: Final commit (docs/notes if any)**

```bash
git add -A
git commit -m "chore: verify GENERAL web-research mission end to end"
```

---

## Self-Review

**Spec coverage:**
- Layer 1 (brain on) → Task 7 step 1 (ADC) + existing planner; planner change in Task 4. ✅
- Layer 2 (Tavily hand) → Tasks 1, 2, 5. ✅
- Layer 3 (hand-off) → Task 6 (human_approval_request reuses dashboard + Telegram). ✅
- Structured tool intents → Tasks 3, 4. ✅
- Error handling (typed errors, never crash) → Task 2 (`_http_error`), Task 6 (`synthesize_and_handoff` swallows + logs). ✅
- Testing (client, planner, executor, synthesis, offline e2e) → Tasks 2, 4, 5, 6, 7. ✅
- Out-of-scope (no browser, no auto-checkout) → respected; nothing added. ✅
- Deployment parity for the Tavily secret → noted as non-blocking follow-up in the spec; not in this plan (intentional). ✅

**Placeholder scan:** No "TBD/TODO/handle edge cases" left in code steps. The two ">" verification notes (Task 5 step 5, Task 6 step 4) ask the engineer to match real signatures rather than invent them — these are guardrails, not placeholders, because the surrounding code is fully specified.

**Type consistency:** `WebSearchClient.search()` returns `SearchResults` (Task 2) and is referenced identically in Task 5's fake and `_resolve_tool`. `Task.tool`/`Task.tool_args` (Task 3) match `TaskDefinition.tool`/`tool_args` (Task 4) and the executor's `task.tool` read (Task 5) and synthesis's `task.result` read (Task 6). `human_approval_request` is consistent across spec and Task 6. ✅
