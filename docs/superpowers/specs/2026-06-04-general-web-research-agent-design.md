# NERVE — General Web-Research Agent (Sub-Project 1)

**Date:** 2026-06-04
**Status:** Approved design — ready for implementation plan
**Author:** brainstormed with the user

---

## 1. Purpose

Make a `GENERAL` NERVE mission do **real web research** for an arbitrary goal and
hand off an actionable recommendation to the user. The motivating example:

> "Find the cheapest 2026 World Cup ticket and a cheap, well-reviewed hotel nearby."

Today a `GENERAL` mission fails at the planning step (no Vertex/ADC credentials)
and, even with the brain connected, has no tools to act on the real world — its
only "hands" are Dynatrace and GitLab. This sub-project connects the brain and
gives it a general-purpose web-research hand, then routes the result through
NERVE's existing human-approval gate as a hand-off.

This is **sub-project 1** of a larger effort. It deliberately ships the smallest
end-to-end slice that works.

## 2. Scope

### In scope (the three layers, minimally)

- **Layer 1 — Brain on.** Verify the existing `MissionPlanner` (direct Gemini
  calls in `orchestrator/planner.py`) produces a sane plan for a `GENERAL` goal.
  This is operational, not a build: it needs Application Default Credentials.
- **Layer 2 — One real hand.** A `WebSearchClient` backed by the Tavily API,
  dispatched by the `ExecutionAgent`. Tavily returns clean page content and
  synthesized answers, so no headless browser is needed for v1.
- **Layer 3 — Hand-off.** After execution, a synthesis step produces a
  recommendation and creates a `human_approval_request` action. This reuses the
  existing approval surface — the dashboard renders it and Telegram DMs it with
  zero new code.

### Out of scope (later sub-projects)

- **Sub-project 2:** headless browse/extract tool (Playwright) for reading
  specific pages deeper than search snippets.
- **Sub-project 3:** deep-link + form pre-fill polish, multi-step booking prep.
- Any **automated checkout / captcha solving / payment entry.** Explicitly
  excluded — ToS-violating, abuse-shaped, and fragile. The user always completes
  the purchase. Layer 3 ends at "tee up the booking and hand off."

## 3. Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Layer 3 boundary | Research + hand-off | Robust, legitimate, reuses approval gate |
| How it reaches the web | General web tools (not vertical APIs) | True "any goal" agent; aligns with NERVE-as-platform |
| Search provider | Tavily (free tier) | Built for AI agents; returns clean, ready-to-read content |
| Browser in v1? | No — Tavily only | Keeps Cloud Run image light; de-risks first build |
| Synthesis location | Small `research_concierge` module | Mirrors `incident_autopilot` workflow pattern |

## 4. Architecture

New and changed components, following existing `mcp_tools/` and `modules/` patterns.

### 4.1 `mcp_tools/web_search.py` — `WebSearchClient` (new)
- Async client; calls the Tavily Search API via `httpx` (consistent with the
  GitLab REST client).
- Public method: `search(query: str, max_results: int | None = None) -> SearchResults`.
- Returns a Pydantic model: a list of results (`title`, `url`, `content`,
  optional `score`) plus an optional synthesized `answer`.
- All failures wrapped in a typed exception (follow `exceptions.py` /
  `MCPError` conventions). Network calls use the project's tenacity retry policy.
- Mirrors the shape and error handling of `mcp_tools/gitlab.py`.

### 4.2 `config.py` + `.env.example` (changed)
- Add `tavily_api_key: str = ""` and `web_search_max_results: int = 5`.
- `.env.example` gains a `# Web Search (Tavily)` block with `TAVILY_API_KEY=`.
- No secret in code or image (invariant #8); Cloud Run injects it from Secret
  Manager in production (add `nerve-tavily-api-key` to `deploy/secrets_setup.sh`
  and `cloudbuild.yaml` — tracked as a follow-up, not blocking local/offline run).

### 4.3 `orchestrator/planner.py` — structured tool intents (changed)
- Extend `TaskDefinition` with optional `tool: str | None = None` and
  `tool_args: dict = {}`, so the planner can emit *executable* steps (e.g.
  `tool="web_search", tool_args={"query": "cheapest 2026 World Cup tickets"}`)
  rather than prose only.
- Update `_PROMPT_TEMPLATE` to tell the planner which tools are available and to
  emit `tool`/`tool_args` for execution-role tasks. Keep the change confined to
  this file (it is the designated "single seam" for planning).
- Backward compatible: incident planning and tasks without a `tool` still work.

### 4.4 `agents/execution_agent.py` — dispatch web_search (changed)
- Extend the tool resolver (currently dynatrace/gitlab) so a task carrying
  `tool="web_search"` resolves to `WebSearchClient.search` with its `tool_args`.
- The agent is constructed with the web-search client alongside its existing
  dynatrace/gitlab clients (wire-up in the orchestrator/app startup).
- Result persistence and retry behavior unchanged — reuses the existing
  `_invoke` / `_call_with_retry` path.

### 4.5 `modules/research_concierge/synthesis.py` — hand-off (new)
- One function, e.g. `synthesize_and_handoff(mission_id, ...)`:
  1. Gathers completed task results for the mission from the state layer.
  2. Makes a single Gemini reasoning call: collected findings → a concise
     recommendation (cheapest option + best-value option + the links).
  3. Creates a `human_approval_request` action whose payload carries the
     recommendation text and the booking/deep links, via the state layer.
  4. Emits the appropriate events (audit invariant #3).
- Mirrors `modules/incident_autopilot/workflow.py`'s
  reasoning → action-generation → approval shape.

### 4.6 Orchestrator wiring (changed, minimal)
- For `mission_type == "GENERAL"`, when all execution tasks have completed, the
  orchestration loop invokes `synthesize_and_handoff` once, then lets the mission
  reach `resolved` (status transitions remain the orchestrator's sole
  responsibility — invariant #4).

## 5. Data flow (end to end)

```
POST /missions {goal, mission_type: "GENERAL"}
  → orchestrator starts the mission loop
  1. MissionPlanner (Gemini) → tasks, execution tasks tagged tool=web_search + query
  2. ExecutionAgent runs each search via Tavily → results saved to task.result
  3. RiskAgent / AuditorAgent cycle as today (adaptation/replanning still apply)
  4. synthesize_and_handoff → recommendation + human_approval_request action
  5. Action surfaces in the dashboard AND pings Telegram (existing surfaces, no new code)
  6. User taps approve → mission resolved; user clicks the link to book
```

## 6. Error handling

- Per `ARCHITECTURE.md`: every external call wrapped; exceptions caught and
  logged at the boundary; typed exceptions, no bare `except`.
- Tavily failures (network, auth, rate limit) raise a typed tool error, are
  retried per policy, and on final failure mark the task `failed` with an
  `error` — the existing risk/replan loop already reacts to failed tasks.
- Missing `TAVILY_API_KEY`: the web-search tool fails fast with a clear typed
  error at first use (not at import), so the rest of the app still boots.
- Synthesis Gemini failure: logged; mission does not hard-crash — it records the
  failure event and the raw task results remain available in state.
- Invariant safety: no secrets in code/images; all writes go through the state
  layer; every event persisted; only the orchestrator changes mission status.

## 7. Testing

- **`WebSearchClient`**: unit tests with `httpx` mocked — success parsing,
  empty results, auth error, network error → typed exception. No live Tavily
  calls in tests.
- **Planner**: a test that a `GENERAL` goal yields tasks carrying
  `tool="web_search"` and a non-empty query (Gemini `generate` fn mocked to
  return canned JSON — same pattern as existing planner tests).
- **ExecutionAgent**: dispatch test — a `web_search` task invokes the
  web-search client with the right args and persists the result.
- **Synthesis**: with canned task results and a mocked Gemini call, asserts a
  `human_approval_request` action is created with the recommendation + links and
  the expected events are emitted.
- **Offline harness**: confirm `run_local_demo.py` can run a `GENERAL` World Cup
  mission end to end with Tavily mocked/stubbed (and, manually, against the real
  Tavily key once set).
- Tests stay hermetic against the developer `.env` (existing conftest overrides).

## 8. The "only the gcloud step is left" goal

After this sub-project, enabling real research requires exactly:
1. `gcloud auth application-default login` (one time — Layer 1 brain).
2. Paste a free `TAVILY_API_KEY` into `.env` (one time — Layer 2 hand).

Then any `GENERAL` goal is researched and handed off. No per-task setup.

## 9. Open follow-ups (not blocking)

- Add `nerve-tavily-api-key` to `deploy/secrets_setup.sh` and `cloudbuild.yaml`
  for Cloud Run parity.
- Sub-project 2: Playwright browse/extract tool.
- Consider per-task LLM tool-selection in the ExecutionAgent (vs. planner-time
  tool intents) if plans need richer mid-execution tool choice.
