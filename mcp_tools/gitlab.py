"""GitLab client and its typed response models.

GitLab has no drop-in MCP server for these operations, so this client talks to
the GitLab REST API (``/api/v4``) directly over httpx — while remaining a
``BaseMCPClient`` subclass so every call still flows through the wrapped path:
audit events (MCP_TOOL_CALLED / MCP_TOOL_RESULT), failure injection, retry, and
typed :class:`~exceptions.MCPError` translation. (Deviation from CLAUDE.md's
"GitLab MCP" wording, flagged because GitLab itself is REST, not MCP.)

Transport: each high-level method calls ``call_tool(<tool>, args)``; the
overridden :meth:`GitLabClient._raw_call` maps that to a real REST request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

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

SERVER_GITLAB = "gitlab"
HTTP_TIMEOUT_SECONDS = 20.0
DEFAULT_TARGET_BRANCH = "main"

TOOL_LIST_DEPLOYMENTS = "list_deployments"
TOOL_GET_COMMIT = "get_commit"
TOOL_GET_COMMIT_DIFF = "get_commit_diff"
TOOL_CREATE_ISSUE = "create_issue"
TOOL_CREATE_MR = "create_merge_request"
TOOL_TRIGGER_PIPELINE = "trigger_pipeline"
TOOL_CLOSE_ISSUE = "close_issue"


class GitLabDeployment(BaseModel):
    """A deployment record for a project environment."""

    model_config = ConfigDict(extra="ignore")

    id: int
    status: str
    ref: str
    sha: str | None = None
    environment: str | None = None
    created_at: datetime | None = None


class GitLabCommit(BaseModel):
    """Commit metadata and (optionally) changed files."""

    model_config = ConfigDict(extra="ignore")

    sha: str
    title: str
    message: str
    author_name: str | None = None
    created_at: datetime | None = None
    web_url: str | None = None
    files_changed: list[str] = Field(default_factory=list)


class GitLabIssue(BaseModel):
    """An issue created or referenced via the API."""

    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    state: str
    web_url: str | None = None
    labels: list[str] = Field(default_factory=list)


class GitLabMR(BaseModel):
    """A merge request."""

    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    state: str
    source_branch: str
    target_branch: str | None = None
    web_url: str | None = None


class GitLabPipeline(BaseModel):
    """A CI/CD pipeline run."""

    model_config = ConfigDict(extra="ignore")

    id: int
    status: str
    ref: str
    sha: str | None = None
    web_url: str | None = None


def _parse_dt(value: Any) -> datetime | None:
    """Parse a GitLab ISO-8601 timestamp string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_list(raw: Any, *keys: str) -> list[dict]:
    """Return the first list found under the given keys (or [])."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []


class GitLabClient(BaseMCPClient):
    """Typed client for the GitLab REST API, wrapped as an MCP-style client."""

    def __init__(
        self,
        *,
        mission_id: str | None = None,
        failure_engine: Any | None = None,
        server_url: str | None = None,
        token: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize from settings unless ``server_url`` overrides the REST base.

        Args:
            mission_id: Mission to attribute audit events to.
            failure_engine: FailureEngine whose modifications to apply.
            server_url: Override the derived ``/api/v4`` base URL.
            token: Override the GitLab private token (defaults to settings).
            **kwargs: Forwarded retry/backoff overrides to the base client.
        """
        base = server_url or f"{settings.gitlab_url.rstrip('/')}/api/v4"
        headers = {"PRIVATE-TOKEN": token if token is not None else settings.gitlab_token}
        super().__init__(
            SERVER_GITLAB,
            base,
            headers,
            mission_id=mission_id,
            failure_engine=failure_engine,
            **kwargs,
        )
        self._http: Any | None = None

    # ----------------------------------------------------------------- #
    # Transport: REST over httpx (overrides the base MCP session)
    # ----------------------------------------------------------------- #
    async def connect(self) -> None:
        """Open the httpx client against the GitLab REST API."""
        import httpx  # lazy: optional dependency, only needed for live calls

        self._http = httpx.AsyncClient(
            base_url=self._server_url, headers=self._auth_headers, timeout=HTTP_TIMEOUT_SECONDS
        )
        self._session = self._http
        self._log.info("gitlab_connected", base_url=self._server_url)

    async def disconnect(self) -> None:
        """Close the httpx client."""
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._session = None
        self._log.info("gitlab_disconnected")

    async def _raw_call(self, tool_name: str, arguments: dict) -> Any:
        """Issue one GitLab REST request for a tool, translating errors.

        Args:
            tool_name: Internal tool name (routed to a REST endpoint).
            arguments: Tool arguments.

        Returns:
            The parsed JSON body (dict or list).

        Raises:
            MCPError: Typed error for transport failures or 4xx/5xx responses.
        """
        if self._http is None:
            raise MCPConnectionError(
                "gitlab client not connected", context={"server": self.server_name, "tool": tool_name}
            )
        method, path, params, body = self._build_request(tool_name, arguments)
        try:
            response = await self._http.request(method, path, params=params, json=body)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 — translate any transport-level error
            raise MCPConnectionError(
                "gitlab request failed",
                context={"server": self.server_name, "tool": tool_name, "error": str(exc)},
                recoverable=True,
            ) from exc
        if response.status_code >= 400:
            raise self._http_error(response.status_code, tool_name)
        return response.json() if response.content else {}

    def _build_request(self, tool: str, args: dict) -> tuple[str, str, dict | None, dict | None]:
        """Map a tool + arguments to (method, path, query params, JSON body)."""
        pid = quote(str(args["project_id"]), safe="")
        if tool == TOOL_LIST_DEPLOYMENTS:
            # GitLab requires order_by=updated_at when filtering with updated_after.
            params = {"order_by": "updated_at", "sort": "desc"}
            if args.get("since"):
                params["updated_after"] = args["since"]
            return "GET", f"/projects/{pid}/deployments", params, None
        if tool == TOOL_GET_COMMIT:
            sha = quote(str(args["commit_sha"]), safe="")
            return "GET", f"/projects/{pid}/repository/commits/{sha}", None, None
        if tool == TOOL_GET_COMMIT_DIFF:
            sha = quote(str(args["commit_sha"]), safe="")
            return "GET", f"/projects/{pid}/repository/commits/{sha}/diff", None, None
        if tool == TOOL_CREATE_ISSUE:
            body = {"title": args["title"], "description": args["description"], "labels": ",".join(args.get("labels") or [])}
            return "POST", f"/projects/{pid}/issues", None, body
        if tool == TOOL_CREATE_MR:
            body = {
                "source_branch": args["source_branch"],
                "target_branch": args.get("target_branch", DEFAULT_TARGET_BRANCH),
                "title": args["title"],
                "description": args["description"],
            }
            return "POST", f"/projects/{pid}/merge_requests", None, body
        if tool == TOOL_TRIGGER_PIPELINE:
            variables = [{"key": k, "value": str(v)} for k, v in (args.get("variables") or {}).items()]
            return "POST", f"/projects/{pid}/pipeline", None, {"ref": args["ref"], "variables": variables}
        if tool == TOOL_CLOSE_ISSUE:
            return "PUT", f"/projects/{pid}/issues/{int(args['issue_iid'])}", None, {"state_event": "close"}
        raise MCPToolCallError(f"unknown GitLab tool: {tool}", context={"tool": tool}, recoverable=False)

    def _http_error(self, status: int, tool: str) -> MCPError:
        """Map a GitLab HTTP status code to a typed MCPError."""
        ctx = {"server": self.server_name, "tool": tool, "status": status}
        if status in (401, 403):
            return MCPAuthError(f"GitLab authentication failed for {tool}", context=ctx)
        if status == 429:
            return MCPRateLimitError(f"GitLab rate limited for {tool}", context=ctx)
        if status >= 500:
            return MCPConnectionError(f"GitLab server error ({status}) for {tool}", context=ctx)
        return MCPToolCallError(f"GitLab returned {status} for {tool}", context=ctx, recoverable=False)

    # ----------------------------------------------------------------- #
    # Typed operations
    # ----------------------------------------------------------------- #
    async def list_recent_deployments(self, project_id: str, since: datetime) -> list[GitLabDeployment]:
        """Return deployments for a project created at or after ``since``.

        Args:
            project_id: GitLab project id (numeric id or URL-encoded path).
            since: Lower bound on deployment time.

        Returns:
            List of :class:`GitLabDeployment`, newest first.
        """
        args = {"project_id": project_id, "since": since.isoformat()}
        raw = await self.call_tool(TOOL_LIST_DEPLOYMENTS, args)
        return [self._parse_deployment(item) for item in _as_list(raw, "deployments", "result", "data")]

    async def get_commit_details(self, project_id: str, commit_sha: str) -> GitLabCommit:
        """Return commit metadata plus its changed files (diff).

        Args:
            project_id: GitLab project id.
            commit_sha: Commit SHA.

        Returns:
            A :class:`GitLabCommit`.
        """
        raw = await self.call_tool(TOOL_GET_COMMIT, {"project_id": project_id, "commit_sha": commit_sha})
        files = await self._fetch_changed_files(project_id, commit_sha)
        try:
            return GitLabCommit(
                sha=str(raw.get("id") or raw.get("sha") or commit_sha),
                title=raw.get("title") or "",
                message=raw.get("message") or "",
                author_name=raw.get("author_name"),
                created_at=_parse_dt(raw.get("created_at")),
                web_url=raw.get("web_url"),
                files_changed=files,
            )
        except PydanticValidationError as exc:
            raise self._parse_error(TOOL_GET_COMMIT, exc) from exc

    async def create_issue(
        self, project_id: str, title: str, description: str, labels: list[str]
    ) -> GitLabIssue:
        """Create a real incident issue.

        Args:
            project_id: GitLab project id.
            title: Issue title.
            description: Issue body (markdown).
            labels: Labels to apply.

        Returns:
            The created :class:`GitLabIssue`.
        """
        args = {"project_id": project_id, "title": title, "description": description, "labels": labels}
        raw = await self.call_tool(TOOL_CREATE_ISSUE, args)
        return self._parse_issue(raw)

    async def create_merge_request(
        self, project_id: str, title: str, description: str, source_branch: str
    ) -> GitLabMR:
        """Create a real merge request from ``source_branch`` into the default branch.

        Args:
            project_id: GitLab project id.
            title: MR title.
            description: MR body (markdown).
            source_branch: Source branch name.

        Returns:
            The created :class:`GitLabMR`.
        """
        args = {"project_id": project_id, "title": title, "description": description, "source_branch": source_branch}
        raw = await self.call_tool(TOOL_CREATE_MR, args)
        try:
            return GitLabMR(
                id=int(raw.get("id")),
                iid=int(raw.get("iid")),
                title=raw.get("title") or title,
                state=raw.get("state") or "opened",
                source_branch=raw.get("source_branch") or source_branch,
                target_branch=raw.get("target_branch"),
                web_url=raw.get("web_url"),
            )
        except (PydanticValidationError, TypeError, ValueError) as exc:
            raise self._parse_error(TOOL_CREATE_MR, exc) from exc

    async def trigger_pipeline(self, project_id: str, ref: str, variables: dict) -> GitLabPipeline:
        """Trigger a real pipeline on a ref (e.g. a rollback pipeline).

        Args:
            project_id: GitLab project id.
            ref: Git ref to run the pipeline on.
            variables: Pipeline variables (sent as key/value pairs).

        Returns:
            The triggered :class:`GitLabPipeline`.
        """
        args = {"project_id": project_id, "ref": ref, "variables": variables}
        raw = await self.call_tool(TOOL_TRIGGER_PIPELINE, args)
        try:
            return GitLabPipeline(
                id=int(raw.get("id")),
                status=raw.get("status") or "created",
                ref=raw.get("ref") or ref,
                sha=raw.get("sha"),
                web_url=raw.get("web_url"),
            )
        except (PydanticValidationError, TypeError, ValueError) as exc:
            raise self._parse_error(TOOL_TRIGGER_PIPELINE, exc) from exc

    async def close_issue(self, project_id: str, issue_iid: int) -> None:
        """Close a real issue (e.g. after the incident resolves).

        Args:
            project_id: GitLab project id.
            issue_iid: Internal issue id (iid) to close.
        """
        await self.call_tool(TOOL_CLOSE_ISSUE, {"project_id": project_id, "issue_iid": issue_iid})

    # ----------------------------------------------------------------- #
    # Parsing
    # ----------------------------------------------------------------- #
    async def _fetch_changed_files(self, project_id: str, commit_sha: str) -> list[str]:
        """Fetch the list of files changed by a commit (best-effort)."""
        try:
            diff = await self.call_tool(TOOL_GET_COMMIT_DIFF, {"project_id": project_id, "commit_sha": commit_sha})
        except MCPError as exc:
            self._log.warning("gitlab_commit_diff_failed", commit=commit_sha, error=str(exc))
            return []
        files: list[str] = []
        for entry in diff if isinstance(diff, list) else []:
            path = entry.get("new_path") or entry.get("old_path")
            if path:
                files.append(path)
        return files

    def _parse_deployment(self, item: dict) -> GitLabDeployment:
        """Parse one deployment record into a model."""
        environment = item.get("environment")
        if isinstance(environment, dict):
            environment = environment.get("name")
        deployable = item.get("deployable") or {}
        try:
            return GitLabDeployment(
                id=int(item.get("id")),
                status=item.get("status") or deployable.get("status") or "unknown",
                ref=item.get("ref") or deployable.get("ref") or "",
                sha=item.get("sha") or deployable.get("sha"),
                environment=environment,
                created_at=_parse_dt(item.get("created_at")),
            )
        except (PydanticValidationError, TypeError, ValueError) as exc:
            raise self._parse_error(TOOL_LIST_DEPLOYMENTS, exc) from exc

    def _parse_issue(self, raw: dict) -> GitLabIssue:
        """Parse an issue payload into a model."""
        labels = raw.get("labels")
        if isinstance(labels, str):
            labels = [label for label in labels.split(",") if label]
        try:
            return GitLabIssue(
                id=int(raw.get("id")),
                iid=int(raw.get("iid")),
                title=raw.get("title") or "",
                state=raw.get("state") or "opened",
                web_url=raw.get("web_url"),
                labels=labels or [],
            )
        except (PydanticValidationError, TypeError, ValueError) as exc:
            raise self._parse_error(TOOL_CREATE_ISSUE, exc) from exc

    def _parse_error(self, tool: str, exc: Exception) -> MCPToolCallError:
        """Build a typed error for a response that failed model validation."""
        return MCPToolCallError(
            "GitLab response failed validation",
            context={"server": self.server_name, "tool": tool, "error": str(exc)},
            recoverable=False,
        )
